"""Xandar — análise temporal do ZIP Harmony (versão local do notebook 03).

Equivale ao notebook 03_Analise_Temporal_Harmony_ZIP.ipynb, mas roda no
Python do computador, sem Colab/navegador. Não usa token e não acessa a NASA.

Uso (PowerShell, dentro da pasta do script):
    python 03_analise_temporal.py                       # procura o ZIP automaticamente
    python 03_analise_temporal.py --zip C:\\caminho\\XANDAR_Harmony_recortes.zip
    python 03_analise_temporal.py --self-test           # só os testes matemáticos

Procura o ZIP, nesta ordem: --zip; .\\XANDAR_Harmony_recortes.zip;
.\\saida\\XANDAR_Harmony\\XANDAR_Harmony_recortes.zip (saída do script 02).
Resultados em .\\saida\\xandar_mudanca e .\\saida\\XANDAR_analise_temporal.zip

Com --config <pasta>\\config.json (gerado pela tela app.py), as cenas/datas vêm
do arquivo, o ZIP é procurado nessa pasta e os resultados ficam nela.
"""

from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent

from pathlib import Path
import json
import math
import sys

CONFIG = {
    "before": {
        "date_utc": "2026-08-19",
        "HHHH": "recortes/19ago_HHHH.tif",
        "HVHV": "recortes/19ago_HVHV.tif",
        "mask": "recortes/19ago_mask.tif",
        # Preencher somente após conferir histogramas e documentação da máscara.
        # Pode ser lista de códigos inteiros ou dict por polarização, se necessário.
        "approved_mask_codes": None,
    },
    "after": {
        "date_utc": "2026-08-31",
        "HHHH": "recortes/31ago_HHHH.tif",
        "HVHV": "recortes/31ago_HVHV.tif",
        "mask": "recortes/31ago_mask.tif",
        "approved_mask_codes": None,
    },
    "output_dir": str(BASE_DIR / "saida" / "xandar_mudanca"),
    # Decisão do analista, após inspecionar as unidades/escala nos metadados:
    "confirmed_linear_power": False,
    "science_quantity": "gamma0_linear",  # editar se a variável documentada diferir
    "mask_code_reference": "",  # URL/versão/seção que justifica os códigos aprovados
    "smooth_power_3x3": True,
    "require_full_window": True,  # nove vizinhos válidos, inclusive centro
    "delta_display_db": [-6.0, 6.0],  # escala da visualização, não limiar de evento
    "intensity_display_db": [-30.0, 0.0],  # apenas visualização; ajustar explicitamente
    "grid_transform_tolerance": 1e-6,
    "maximum_pixels": 4_000_000,
    # Opcional: arquivo GeoJSON Polygon/MultiPolygon. None analisa todo o recorte.
    "aoi_geojson": None,
    "aoi_crs": "EPSG:4326",
    "write_cog": True,
}


def delta_scalar(after, before):
    """Fórmula independente de bibliotecas para teste de regressão matemática."""
    if not all(math.isfinite(v) and v > 0 for v in (after, before)):
        raise ValueError("Potências devem ser positivas e finitas.")
    return 10.0 * math.log10(after / before)


def validate_grid(reference, other, tolerance=1e-6):
    """Recusa mistura de CRS/grades; não faz alinhamento ou interpolação implícitos."""
    if reference["crs"] != other["crs"]:
        raise ValueError("CRS diferentes: alinhe previamente os recortes e documente a operação.")
    if (reference["height"], reference["width"]) != (other["height"], other["width"]):
        raise ValueError("Dimensões diferentes: não subtrair estes rasters diretamente.")
    a, b = reference["transform"], other["transform"]
    if len(a) != 6 or len(b) != 6 or any(abs(float(x)-float(y)) > tolerance for x, y in zip(a, b)):
        raise ValueError("Transformações diferentes: grades não coincidentes.")


def self_test():
    import unittest

    class MathTests(unittest.TestCase):
        def test_double_power(self):
            self.assertAlmostEqual(delta_scalar(2.0, 1.0), 3.01029995664, places=9)

        def test_equal_power(self):
            self.assertAlmostEqual(delta_scalar(1.25, 1.25), 0.0, places=12)

        def test_invalid_power(self):
            for after, before in [(0, 1), (2, -1), (float("nan"), 1)]:
                with self.assertRaises(ValueError):
                    delta_scalar(after, before)

        def test_grid_mismatch(self):
            a = {"crs": "EPSG:32645", "height": 3, "width": 3,
                 "transform": (10, 0, 1000, 0, -10, 2000)}
            validate_grid(a, dict(a))
            for other in [dict(a, crs="EPSG:4326"), dict(a, width=4),
                          dict(a, transform=(10, 0, 1010, 0, -10, 2000))]:
                with self.assertRaises(ValueError):
                    validate_grid(a, other)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MathTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


def executar_analise(config):
    import copy
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom
    from rasterio.shutil import copy as raster_copy
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    config = copy.deepcopy(config)
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    inputs, records = {}, {}
    reference = None

    def serial_value(value):
        if isinstance(value, dict):
            return {str(k): serial_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [serial_value(v) for v in value]
        if isinstance(value, np.generic):
            return serial_value(value.item())
        if isinstance(value, float) and not math.isfinite(value):
            return "NaN" if math.isnan(value) else str(value)
        return value

    def read_raster(path, science):
        nonlocal reference
        with rasterio.open(path) as src:
            if src.crs is None:
                raise ValueError(f"Raster sem CRS: {Path(path).name}")
            grid = {"crs": src.crs.to_wkt(), "height": src.height, "width": src.width,
                    "transform": list(src.transform)[:6]}
            if (abs(src.transform.b) > config["grid_transform_tolerance"] or
                    abs(src.transform.d) > config["grid_transform_tolerance"] or
                    src.transform.a <= 0 or src.transform.e >= 0):
                raise ValueError("Use rasters north-up sem rotação. A visualização por extensão não é adequada a esta grade.")
            if src.width * src.height > config["maximum_pixels"]:
                raise ValueError("Entrada muito grande. Use os recortes, não a cena integral.")
            if reference is None:
                reference = grid
            else:
                validate_grid(reference, grid, config["grid_transform_tolerance"])
            dtype = np.dtype(src.dtypes[0])
            if science and not (dtype.kind == "f" and dtype.itemsize == 4):
                raise ValueError(f"{Path(path).name}: a variável científica deve ser Float32; PNG/Byte não aceito.")
            if not science and dtype.kind not in "ui":
                raise ValueError("A máscara deve conter códigos inteiros, não imagem de visualização.")
            raw = src.read(1, masked=True)
            invalid = np.ma.getmaskarray(raw)
            scale, offset = float(src.scales[0]), float(src.offsets[0])
            if not math.isfinite(scale) or not math.isfinite(offset):
                raise ValueError("Escala/offset não finitos nos metadados.")
            if not science and (scale != 1 or offset != 0):
                raise ValueError("Máscara com scale/offset não triviais: inspecione a codificação antes de continuar.")
            values = np.asarray(raw.data, dtype="float64") if science else np.asarray(raw.data)
            if science:
                values = values * scale + offset
            metadata = {
                "path": str(path), "grid": grid, "dtype": src.dtypes[0], "bands": src.count,
                "band_used": 1, "nodata": serial_value(src.nodatavals[0]),
                "scale_applied": scale, "offset_applied": offset,
                "units": src.units[0], "dataset_tags": src.tags(), "band_tags": src.tags(1),
            }
            return values, invalid, metadata

    # Inspection is deliberately first, before authorizing any mask codes.
    for period in ("before", "after"):
        spec = config[period]
        inputs[period], records[period] = {}, {}
        for layer in ("HHHH", "HVHV", "mask"):
            inputs[period][layer] = read_raster(spec[layer], layer != "mask")
            records[period][layer] = inputs[period][layer][2]
        mask, mask_missing, _ = inputs[period]["mask"]
        codes, counts = np.unique(mask[~mask_missing], return_counts=True)
        histogram = {str(int(code)): int(count) for code, count in zip(codes, counts)}
        records[period]["mask"]["histogram_excluding_nodata"] = histogram
        print(f"{period} ({spec['date_utc']}) — histograma dos códigos de máscara:")
        print(json.dumps(histogram, indent=2))
        print("Tags da máscara:", json.dumps(records[period]["mask"]["band_tags"], ensure_ascii=False))
        for layer in ("HHHH", "HVHV"):
            metadata = records[period][layer]
            print(f"  {layer}: unidades={metadata['units']!r}; scale={metadata['scale_applied']}; offset={metadata['offset_applied']}")
    inspection = {"inputs": records, "note": "Histograma não define quais códigos são válidos. Consulte a especificação do produto e QA."}
    (output / "inspecao_entradas.json").write_text(json.dumps(serial_value(inspection), ensure_ascii=False, indent=2), encoding="utf-8")

    # Gate: no universal assumption such as mask==0 valid.
    def approved_codes(period, layer):
        codes = config[period]["approved_mask_codes"]
        if isinstance(codes, dict):
            codes = codes.get(layer)
        if codes is None or not isinstance(codes, (list, tuple)) or not codes:
            raise ValueError("Códigos de máscara não aprovados. Veja inspecao_entradas.json, confira a especificação/QA e preencha approved_mask_codes em cada data. Nenhum código foi assumido válido.")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in codes):
            raise ValueError("approved_mask_codes deve conter códigos inteiros aprovados pelo analista.")
        return codes

    for period in ("before", "after"):
        for layer in ("HHHH", "HVHV"):
            approved_codes(period, layer)
    if not config.get("mask_code_reference"):
        raise ValueError("Registre em mask_code_reference a fonte/versão que justifica os códigos de máscara.")
    if not config.get("confirmed_linear_power") or not config.get("science_quantity"):
        raise ValueError("Confirme que as camadas representam potência linear e registre a grandeza/unidade. Não usar amplitude, dB ou PNG nesta fórmula.")

    # Use source metadata for extent and all optional AOI rasterization.
    with rasterio.open(config["before"]["HHHH"]) as src:
        profile = src.profile.copy()
        transform, crs, extent = src.transform, src.crs, tuple(src.bounds)
    shape = (reference["height"], reference["width"])
    aoi = np.ones(shape, dtype=bool)
    geometry_record = None
    if config.get("aoi_geojson"):
        obj = json.loads(Path(config["aoi_geojson"]).read_text(encoding="utf-8-sig"))
        if obj.get("type") == "Feature":
            obj = obj["geometry"]
        if obj.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("AOI deve ser Polygon/MultiPolygon ou Feature com essa geometria.")
        projected = transform_geom(config["aoi_crs"], crs, obj)
        aoi = geometry_mask([projected], out_shape=shape, transform=transform,
                            invert=True, all_touched=False)
        geometry_record = {"crs": config["aoi_crs"], "geometry": obj, "pixel_rule": "centro do pixel dentro da AOI"}
    aoi_count = int(aoi.sum())
    if not aoi_count:
        raise ValueError("AOI sem pixels dentro do recorte.")

    def smooth(array, valid):
        if not config["smooth_power_3x3"]:
            return array, valid
        # Moving mean in POWER. All nine neighbors are required by default.
        padded_values = np.pad(np.where(valid, array, 0.0), 1, mode="constant")
        padded_valid = np.pad(valid.astype("uint8"), 1, mode="constant")
        sums = np.zeros(shape, dtype="float64")
        support = np.zeros(shape, dtype="uint8")
        for iy in range(3):
            for ix in range(3):
                sums += padded_values[iy:iy+shape[0], ix:ix+shape[1]]
                support += padded_valid[iy:iy+shape[0], ix:ix+shape[1]]
        accepted = valid & (support == 9 if config["require_full_window"] else support > 0)
        mean = np.full(shape, np.nan, dtype="float64")
        np.divide(sums, support, out=mean, where=accepted)
        return mean, accepted

    def save_science(path, array, tags):
        # Rasterio writes partial artifacts under a temporary name; no partial final TIFF.
        temporary = path.with_suffix(".partial.tif")
        profile.update(driver="GTiff", dtype="float32", count=1, nodata=float("nan"),
                       compress="deflate", tiled=True, blockxsize=256, blockysize=256)
        # Strip source options that may be invalid for a single Float32 band.
        for key in ("photometric", "interleave"):
            profile.pop(key, None)
        with rasterio.open(temporary, "w", **profile) as dst:
            dst.write(array.astype("float32"), 1)
            dst.set_band_unit(1, "dB")
            dst.update_tags(**tags)
        if config["write_cog"]:
            cog = path.with_suffix(".cog.partial.tif")
            raster_copy(str(temporary), str(cog), driver="COG", COMPRESS="DEFLATE",
                        BLOCKSIZE=256, OVERVIEW_RESAMPLING="AVERAGE")
            with rasterio.open(cog) as check:
                if check.dtypes[0] != "float32" or check.crs != crs or check.transform != transform:
                    raise ValueError("COG de saída não preservou tipo ou grade.")
            cog.replace(path)
            temporary.unlink()
        else:
            temporary.replace(path)

    summaries = {}
    vmin, vmax = map(float, config["delta_display_db"])
    if not (vmin < 0 < vmax and math.isclose(-vmin, vmax)):
        raise ValueError("Escala divergente deve ser explícita e simétrica, por exemplo [-6,6].")
    power_min, power_max = map(float, config["intensity_display_db"])
    if power_min >= power_max:
        raise ValueError("Escala de intensidade inválida.")
    display_extent = [extent[0], extent[2], extent[1], extent[3]]

    for layer in ("HHHH", "HVHV"):
        prepared, valid_by_date = {}, {}
        initial_counts = {}
        for period in ("before", "after"):
            array, missing, _ = inputs[period][layer]
            mask, mask_missing, _ = inputs[period]["mask"]
            valid = (~missing & ~mask_missing & np.isfinite(array) & (array > 0) & aoi
                     & np.isin(mask, approved_codes(period, layer)))
            initial_counts[period] = int(valid.sum())
            prepared[period], valid_by_date[period] = smooth(array, valid)
        common = valid_by_date["before"] & valid_by_date["after"] & aoi
        if not np.any(common):
            raise ValueError(f"{layer}: nenhum pixel comum válido após os filtros. Não gerar interpretação.")
        delta = np.full(shape, np.nan, dtype="float64")
        # Stable log-domain difference, mathematically equivalent to log(after/before).
        delta[common] = (10 * np.log10(prepared["after"][common])
                         - 10 * np.log10(prepared["before"][common]))
        values = delta[common]
        median = float(np.median(values))
        summaries[layer] = {
            "pixels_aoi": aoi_count, "valid_before_smoothing": initial_counts,
            "common_valid_pixels": int(common.sum()),
            "common_valid_percent_of_aoi": float(common.sum()/aoi_count*100),
            "median_db": median, "mad_db": float(np.median(np.abs(values-median))),
            "percentiles_db": {str(p):float(np.percentile(values,p)) for p in (1,5,25,50,75,95,99)},
            "note": "MAD descritiva, não erro-padrão ou intervalo de confiança; pixels vizinhos podem ser correlacionados.",
        }
        tif = output / f"delta_{layer}_dB.tif"
        save_science(tif, delta, {"formula":"10*log10(power_after/power_before)",
                                "science_quantity":config["science_quantity"], "units":"dB",
                                "before_utc":config["before"]["date_utc"], "after_utc":config["after"]["date_utc"],
                                "classification":"none", "display_limits_db":json.dumps([vmin,vmax]),
                                "smoothing":"mean power 3x3" if config["smooth_power_3x3"] else "none"})
        fig, axes = plt.subplots(1,3,figsize=(15,5), constrained_layout=True)
        for axis, period in zip(axes[:2], ("before","after")):
            image = np.full(shape,np.nan)
            image[common] = 10*np.log10(prepared[period][common])
            shown = axis.imshow(image, cmap="gray", vmin=power_min, vmax=power_max,
                                extent=display_extent, origin="upper")
            axis.set_title(f"{config[period]['date_utc']} — {layer}")
            fig.colorbar(shown,ax=axis,label=f"{config['science_quantity']} (dB)",shrink=.75)
        shown=axes[2].imshow(delta, cmap="RdBu_r",vmin=vmin,vmax=vmax,
                            extent=display_extent, origin="upper")
        axes[2].set_title("Depois − antes; sem classificação")
        fig.colorbar(shown,ax=axes[2],label="Δ retroespalhamento (dB)",shrink=.75)
        for axis in axes:
            axis.set_xlabel(f"X — {crs.to_string()}")
            axis.set_ylabel("Y")
        fig.suptitle("GCOV PROVISIONAL — comparação descritiva; sombras/relevo/umidade podem alterar o sinal")
        fig.savefig(output/f"comparacao_{layer}.png",dpi=160)
        plt.close(fig)
        print(layer, json.dumps(summaries[layer],ensure_ascii=False,indent=2))

    manifest = {
        "configuration":config, "inputs":records, "aoi":geometry_record,
        "statistics":summaries, "output_units":"dB",
        "formula":"10*log10(after_power/before_power)",
        "radiometric_operation":"source_scale_and_offset applied, then mean in linear power when selected",
        "display_thresholds_are_not_event_thresholds":True,
        "smoothing_warning":"3×3 is a moving mean; it does not create nine independent looks or establish a new independent spatial resolution.",
        "classification":"none: no inundation, movement, deformation or instability classification",
        "interpretation_limit":"Não calcular área do desastre a partir destes mapas. Validar qualidade, máscaras, precipitação, neve, atmosfera/geométria pertinentes e observações independentes.",
        "versions":{"numpy":np.__version__,"rasterio":rasterio.__version__,"matplotlib":matplotlib.__version__},
    }
    (output/"manifesto_analise_temporal.json").write_text(json.dumps(serial_value(manifest),ensure_ascii=False,indent=2),encoding="utf-8")
    print("Análise concluída. Mapas e manifesto em",output)
    return manifest



# ----------------------------------------------------------------------------
# Etapa 3 do notebook: conferir o ZIP e preencher CONFIG (sem upload do Colab)
# ----------------------------------------------------------------------------
EXPECTED_SCENES = {
    '2026-08-19': 'NISAR_L2_PR_GCOV_028_098_A_016_4005_DHDH_A_20260819T233918_20260819T233953_X05026_N_F_J_001',
    '2026-08-31': 'NISAR_L2_PR_GCOV_029_098_A_016_4005_DHDH_A_20260831T233918_20260831T233952_X05026_N_F_J_001',
}
DATA_ANTES, DATA_DEPOIS = '2026-08-19', '2026-08-31'


def carregar_config(path):
    """Substitui cenas e datas antes/depois pelos valores do config.json."""
    global EXPECTED_SCENES, DATA_ANTES, DATA_DEPOIS
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    EXPECTED_SCENES = {str(k): str(v) for k, v in cfg['scenes'].items()}
    comp = cfg.get('comparacao') or {}
    DATA_ANTES = comp.get('antes') or min(EXPECTED_SCENES)
    DATA_DEPOIS = comp.get('depois') or max(EXPECTED_SCENES)
    if DATA_ANTES not in EXPECTED_SCENES or DATA_DEPOIS not in EXPECTED_SCENES or DATA_ANTES >= DATA_DEPOIS:
        raise RuntimeError('Datas antes/depois inválidas no config.')
    CONFIG['before']['date_utc'] = DATA_ANTES
    CONFIG['after']['date_utc'] = DATA_DEPOIS
    print(f'Config: {path}\n  antes={DATA_ANTES}  depois={DATA_DEPOIS}')
    return cfg


def localizar_zip(argumento, pasta=None):
    candidatos = [Path(argumento)] if argumento else ([pasta / 'XANDAR_Harmony_recortes.zip'] if pasta else []) + [
        BASE_DIR / 'XANDAR_Harmony_recortes.zip',
        BASE_DIR / 'saida' / 'XANDAR_Harmony' / 'XANDAR_Harmony_recortes.zip',
    ]
    for c in candidatos:
        if c.is_file():
            return c.resolve()
    raise SystemExit('ZIP não encontrado. Procurado em:\n  ' + '\n  '.join(map(str, candidatos))
                     + '\nInforme com: python 03_analise_temporal.py --zip <caminho>')


def conferir_zip(zip_path, folder):
    import zipfile, hashlib
    import numpy as np
    import rasterio

    folder.mkdir(parents=True, exist_ok=True)
    mapping, grids, metadata = {}, [], []
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            raise RuntimeError('Falha de integridade no ZIP.')
        manifest = json.loads(z.read('manifesto_harmony.json'))
        if manifest['scenes'] != EXPECTED_SCENES or manifest['collection'] != 'NISAR_L2_GCOV_PROVISIONAL_V1':
            raise RuntimeError('O ZIP não corresponde às cenas GCOV esperadas.')
        entries = manifest['files']
        if len(entries) != 3 * len(EXPECTED_SCENES):
            raise RuntimeError(f'Esperados exatamente {3 * len(EXPECTED_SCENES)} recortes.')
        for item in entries:
            date, layer = item['date_utc'], item['layer']
            key = (date, layer)
            if date not in EXPECTED_SCENES or layer not in ['HHHH', 'HVHV', 'mask'] or key in mapping:
                raise RuntimeError('Camada/data inválida ou duplicada.')
            body = z.read(item['file'].replace('\\', '/'))
            if hashlib.sha256(body).hexdigest() != item['sha256'] or len(body) != item['bytes']:
                raise RuntimeError('Hash/tamanho não confere com o manifesto.')
            path = folder / (date + '_' + layer + '.tif')
            path.write_bytes(body)
            with rasterio.open(path) as ds:
                if ds.crs is None or ds.count != 1:
                    raise RuntimeError('Georreferência ou bandas inesperadas.')
                grids.append((ds.crs.to_string(), ds.width, ds.height, tuple(ds.transform)))
                tags = ds.tags(1)
                if layer == 'mask':
                    if ds.dtypes[0] != 'uint8':
                        raise RuntimeError('Tipo da máscara inesperado.')
                    description = tags.get('description', '').lower()
                    if not all(text in description for text in ['subswath', 'fully-focused', 'value 0', '255']):
                        raise RuntimeError('Descrição da máscara não corresponde à interpretação verificada.')
                    values, counts = np.unique(ds.read(1), return_counts=True)
                    if not set(map(int, values)).issubset({0, 1, 255}) or 1 not in values:
                        raise RuntimeError('Códigos da máscara mudaram: revisar antes de aprovar.')
                    print(date, 'máscara:', dict(zip(map(int, values), map(int, counts))))
                else:
                    if ds.dtypes[0] != 'float32' or tags.get('units') != '1' or layer not in tags.get('long_name', ''):
                        raise RuntimeError('Camada científica não corresponde ao GCOV Float32 esperado.')
                    if ds.scales[0] != 1 or ds.offsets[0] != 0:
                        raise RuntimeError('Escala/offset mudaram: revisar interpretação.')
                metadata.append({'date': date, 'layer': layer, 'file': str(path), 'band_tags': tags})
            mapping[key] = str(path)
    if len(set(grids)) != 1:
        raise RuntimeError('As grades não coincidem (cenas de trilhas/frames diferentes?). Não comparar pixels diretamente.')
    (folder / 'metadados_mascara_verificados.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(grids)} recortes verificados. Mesma grade:', grids[0][:3])
    return mapping


def preencher_config(mapping, output_dir):
    CONFIG['before'].update({layer: mapping[(DATA_ANTES, layer)] for layer in ['HHHH', 'HVHV', 'mask']})
    CONFIG['after'].update({layer: mapping[(DATA_DEPOIS, layer)] for layer in ['HHHH', 'HVHV', 'mask']})
    for period in ['before', 'after']:
        CONFIG[period]['approved_mask_codes'] = [1]
    CONFIG.update({
        'output_dir': str(output_dir),
        'confirmed_linear_power': True,
        'science_quantity': 'gamma0_linear',
        'mask_code_reference': 'Descrição original preservada nas tags das duas máscaras GCOV deste ZIP: subswath válido;0 inválido;255 fill. Ver metadados_mascara_verificados.json.',
        'smooth_power_3x3': True,
        'require_full_window': True,
        'delta_display_db': [-6.0, 6.0],
        'intensity_display_db': [-30.0, 0.0],
        'write_cog': True,
    })


def empacotar(output_dir, archive):
    import zipfile
    names = ["inspecao_entradas.json", "manifesto_analise_temporal.json"]
    for layer in ("HHHH", "HVHV"):
        names += [f"delta_{layer}_dB.tif", f"comparacao_{layer}.png"]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as z:
        for name in names:
            z.write(output_dir / name, name)
    print('Pacote de resultados:', archive)


def cli():
    import argparse, os
    parser = argparse.ArgumentParser(description='Análise temporal dos recortes GCOV (versão local).')
    parser.add_argument('--zip', help='Caminho do XANDAR_Harmony_recortes.zip')
    parser.add_argument('--saida', type=Path, default=BASE_DIR / 'saida', help='Pasta base de saída (padrão: .\\saida)')
    parser.add_argument('--self-test', action='store_true', help='Executa só os testes matemáticos.')
    parser.add_argument('--config', type=Path, help='config.json gerado pela tela (app.py).')
    parser.add_argument('--nao-abrir', action='store_true', help='Não abrir as figuras ao final (Windows).')
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    base = args.saida.resolve()
    pasta_cfg = None
    if args.config:
        carregar_config(args.config.resolve())
        pasta_cfg = args.config.resolve().parent
        if args.saida == parser.get_default('saida'):
            base = pasta_cfg
    zip_path = localizar_zip(args.zip, pasta_cfg)
    print('ZIP:', zip_path)
    mapping = conferir_zip(zip_path, base / 'xandar_harmony_verificado')
    output_dir = base / 'xandar_mudanca'
    preencher_config(mapping, output_dir)
    print('Calculando diferenças de intensidade em potência (não é deformação)...')
    executar_analise(CONFIG)
    empacotar(output_dir, base / 'XANDAR_analise_temporal.zip')
    figuras = [output_dir / f'comparacao_{layer}.png' for layer in ('HHHH', 'HVHV')]
    for f in figuras:
        print('Figura:', f)
    if os.name == 'nt' and not args.nao_abrir:
        for f in figuras:
            os.startfile(f)  # abre no visualizador de imagens padrão


if __name__ == '__main__':
    try:
        cli()
    except (RuntimeError, ValueError) as e:
        sys.exit(f'\nERRO: {e}')
