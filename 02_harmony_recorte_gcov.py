"""XANDAR — recortes NISAR com NASA Harmony (versão local do notebook 02).

Equivale ao notebook 02_Harmony_Recorte_GCOV.ipynb, mas roda no Python do
computador, sem Colab/navegador.

Uso (PowerShell, dentro da pasta do script):
    python 02_harmony_recorte_gcov.py                # teste (19/08 HHHH) -> pergunta -> expansão -> ZIP
    python 02_harmony_recorte_gcov.py --etapa teste  # só o teste inicial
    python 02_harmony_recorte_gcov.py --etapa expansao  # expansão + verificação + ZIP
    python 02_harmony_recorte_gcov.py --etapa zip    # só verificação + ZIP (sem token)

Token EDL: digitado em campo oculto, ou lido da variável de ambiente EDL_TOKEN.
Os resultados ficam em .\\saida\\XANDAR_Harmony (altere com --saida).

Com --config <pasta>\\config.json (gerado pela tela app.py), a área (bbox) e as
cenas vêm do arquivo, e os resultados ficam na pasta do config.
Reexecutar retoma jobs já registrados no manifesto, sem reenviar pedidos.
"""
from pathlib import Path
import argparse
import os, sys, json, time, hashlib, re, inspect, zipfile
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote
import importlib.metadata as im

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if sys.version_info < (3, 11):
    sys.exit("Use Python 3.11 ou mais novo (hashlib.file_digest).")

import requests
import numpy as np
import rasterio
import rasterio.windows

BASE_DIR = Path(__file__).resolve().parent

COLLECTION = 'NISAR_L2_GCOV_PROVISIONAL_V1'
COLLECTION_ID = 'C2854338529-ASF'
BBOX = (85.4772, 28.2437, 85.5792, 28.3337)  # oeste, sul, leste, norte
SCENES = {
    '2026-08-19': 'NISAR_L2_PR_GCOV_028_098_A_016_4005_DHDH_A_20260819T233918_20260819T233953_X05026_N_F_J_001',
    '2026-08-31': 'NISAR_L2_PR_GCOV_029_098_A_016_4005_DHDH_A_20260831T233918_20260831T233952_X05026_N_F_J_001',
}
VARIABLES = {
    'HHHH': '/science/LSAR/GCOV/grids/frequencyA/HHHH',
    'HVHV': '/science/LSAR/GCOV/grids/frequencyA/HVHV',
    'mask': '/science/LSAR/GCOV/grids/frequencyA/mask',
}
CAPABILITIES_URL = 'https://harmony.earthdata.nasa.gov/capabilities'

RESULT_DIR: Path = None
MANIFEST_PATH: Path = None
manifest: dict = None
client = None


def primeira_data():
    return min(SCENES)


def carregar_config(path: Path):
    """Substitui BBOX e SCENES pelos valores do config.json do projeto."""
    global BBOX, SCENES
    cfg = json.loads(path.read_text(encoding='utf-8'))
    bbox = tuple(float(v) for v in cfg['bbox'])
    if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise RuntimeError('bbox inválido no config (oeste, sul, leste, norte).')
    scenes = {str(k): str(v) for k, v in cfg['scenes'].items()}
    if len(scenes) < 1:
        raise RuntimeError('Nenhuma cena no config.')
    if cfg.get('collection_id', COLLECTION_ID) != COLLECTION_ID:
        raise RuntimeError('Coleção do config difere da suportada por este script.')
    BBOX, SCENES = bbox, scenes
    print('Config:', path)
    print('  bbox:', BBOX)
    for d, g in SCENES.items():
        print('  cena', d, g)
    return cfg


def now():
    return datetime.now(timezone.utc).isoformat()


def save_manifest():
    tmp = MANIFEST_PATH.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(MANIFEST_PATH)


# ---------------------------------------------------------------- 2. Destino
def preparar_destino(result_dir: Path):
    global RESULT_DIR, MANIFEST_PATH, manifest
    RESULT_DIR = result_dir
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH = RESULT_DIR / 'manifesto_harmony.json'
    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
        if manifest.get('collection') != COLLECTION or tuple(manifest.get('bbox_wgs84', [])) != BBOX:
            raise RuntimeError('Manifesto pertence a outra coleção/área. Escolha outra pasta; não o sobrescreva.')
        if manifest.get('scenes') != SCENES:
            raise RuntimeError('Manifesto desta pasta foi feito para outras cenas. Crie um projeto novo (outro nome).')
    else:
        manifest = {
            'created_utc': now(), 'collection': COLLECTION, 'collection_id': COLLECTION_ID,
            'bbox_wgs84': list(BBOX), 'scenes': SCENES,
            'aoi_note': 'Retângulo proposto ~10 × 10 km ao redor da origem; não é a delimitação do desastre.',
            'jobs': {}, 'files': [], 'grid_checks': {},
            'evidence': 'Capacidades consultadas; extração autenticada depende desta execução.',
        }
    manifest['versions'] = {p: im.version(p) for p in ['harmony-py', 'rasterio', 'numpy', 'requests']}
    save_manifest()
    print('Destino:', RESULT_DIR)
    print('Jobs já registrados:', len(manifest['jobs']))
    print('Versões:', manifest['versions'])


# ---------------------------------------------------------- 3. Capacidades
def checar_capacidades():
    response = requests.get(CAPABILITIES_URL, params={'shortName': COLLECTION}, timeout=(20, 60))
    response.raise_for_status()
    capabilities = response.json()
    if capabilities.get('conceptId') != COLLECTION_ID:
        raise RuntimeError('Identificador da coleção mudou; revisar a configuração antes de submeter jobs.')
    services = capabilities.get('services', [])
    scientific = [s for s in services if s.get('name') == 'nasa/harmony-gdal-adapter']
    if len(scientific) != 1:
        raise RuntimeError('Serviço científico Harmony não encontrado. Não foi iniciado download.')
    service_caps = scientific[0].get('capabilities', {})
    subset = service_caps.get('subsetting', {})
    formats = {f.get('mimeType') for f in service_caps.get('outputFormats', [])}
    available_variables = {v['name'] for v in capabilities.get('variables', [])}
    if not subset.get('bbox') or not subset.get('variable') or 'image/tiff' not in formats:
        raise RuntimeError('A coleção não declara a combinação bbox + variável + TIFF.')
    if not set(VARIABLES.values()).issubset(available_variables):
        raise RuntimeError('Faltam variáveis esperadas nas capacidades. Revisar antes de prosseguir.')
    (RESULT_DIR / 'capabilities.json').write_text(json.dumps(capabilities, ensure_ascii=False, indent=2), encoding='utf-8')
    manifest['capabilities_checked_utc'] = now()
    manifest['scientific_service'] = scientific[0]['name']
    save_manifest()
    print('Confirmado: bbox + seleção de variáveis + image/tiff.')
    print('Polígono arbitrário suportado:', bool(subset.get('shape')))


# ---------------------------------------------------------- 4. Autenticar
def autenticar():
    global client
    os.environ['NUM_REQUESTS_WORKERS'] = '1'
    from harmony import Client, Request
    from getpass import getpass

    required_client = {'token', 'download_timeout'}
    required_request = {'spatial', 'variables', 'granule_name', 'format', 'max_results', 'ignore_errors'}
    if not required_client.issubset(inspect.signature(Client).parameters):
        raise RuntimeError('Versão instalada do harmony-py não possui a assinatura esperada. Atualize: pip install -U harmony-py')
    if not required_request.issubset(inspect.signature(Request).parameters):
        raise RuntimeError('Assinatura Request diferente da verificada. Não submeter automaticamente.')

    edl_token = os.environ.get('EDL_TOKEN', '').strip()
    if edl_token:
        print('Usando token da variável de ambiente EDL_TOKEN.')
    else:
        edl_token = getpass('Token EDL (entrada oculta, sem Bearer): ').strip()
    if not edl_token or any(c.isspace() for c in edl_token):
        raise RuntimeError('Token vazio ou contém espaços. Cole apenas o token.')
    try:
        client = Client(token=edl_token, check_interval=10.0, download_timeout=(20, 120))
    except Exception:
        raise RuntimeError('Autenticação Harmony falhou. Verifique validade do token, aplicação e termos EDL. Nenhum job foi submetido.') from None
    finally:
        edl_token = None
    print('Cliente Harmony configurado. O token fica somente na memória deste processo.')


def encerrar_cliente():
    global client
    if client is None:
        return
    if getattr(client, 'session', None) is not None:
        client.session.close()
    client.token = None
    client.auth = None
    if hasattr(client, 'executor'):
        client.executor.shutdown(wait=True)
    client = None
    print('Cliente encerrado. Resultados e manifesto preservados em', RESULT_DIR)


# ------------------------------------------------ 5. Acompanhamento/download
def safe_tags(tags):
    forbidden = re.compile(r'token|password|authorization|signature|credential|secret', re.I)
    return {str(k): str(v) for k, v in tags.items()
            if not forbidden.search(str(k)) and not forbidden.search(str(v))}


def serial_nodata(v):
    if v is None:
        return None
    if isinstance(v, (float, np.floating)) and not np.isfinite(v):
        return str(v)
    return v.item() if hasattr(v, 'item') else v


def infer_layer(path, tags, requested):
    text = unquote(path.name) + ' ' + json.dumps(tags)
    found = [name for name in VARIABLES
             if re.search(r'(^|[/_.\s-])' + re.escape(name) + r'([/_.\s-]|$)', text, re.I)]
    return found[0] if len(found) == 1 else None


def inspect_tiff(path, date, job_id, requested):
    with rasterio.open(path) as ds:
        if ds.driver != 'GTiff' or ds.crs is None or ds.width == 0 or ds.height == 0:
            raise RuntimeError('Saída não é um GeoTIFF georreferenciado válido.')
        tags = safe_tags(ds.tags())
        structure = safe_tags(ds.tags(ns='IMAGE_STRUCTURE'))
        info = {
            # as_posix(): barras "/" mesmo no Windows, para o ZIP e o notebook/script 03.
            'file': path.relative_to(RESULT_DIR).as_posix(), 'date_utc': date, 'job_id': job_id,
            'layer': infer_layer(path, tags, requested), 'requested_layers': requested,
            'bytes': path.stat().st_size, 'driver': ds.driver, 'dtype': list(ds.dtypes),
            'crs': ds.crs.to_string(), 'epsg': ds.crs.to_epsg(),
            'transform': list(ds.transform)[:6], 'width': ds.width, 'height': ds.height,
            'resolution': list(ds.res), 'bands': ds.count, 'nodata': [serial_nodata(x) for x in ds.nodatavals],
            'tiled': bool(ds.profile.get('tiled')), 'block_shapes': [list(x) for x in ds.block_shapes],
            'image_structure': structure, 'cog_layout_declared': structure.get('LAYOUT') == 'COG',
            'overviews': ds.overviews(1), 'tags': tags,
            'mask_flags': [[flag.name for flag in flags] for flags in ds.mask_flag_enums],
        }
        sample = ds.read(1, window=rasterio.windows.Window(0, 0, min(32, ds.width), min(32, ds.height)))
        info['sample_readable'] = sample.size > 0
    with path.open('rb') as source:
        info['sha256'] = hashlib.file_digest(source, 'sha256').hexdigest()
    print(f"  {info['layer'] or 'CAMADA NÃO IDENTIFICADA'}: {info['width']} × {info['height']}, "
          f"{info['dtype']}, {info['crs']}, {info['bytes']/1024**2:.2f} MiB; COG declarado={info['cog_layout_declared']}")
    return info


def key_for(date, layers):
    specification = json.dumps({'scene': SCENES[date], 'bbox': BBOX, 'layers': sorted(layers),
                                'format': 'image/tiff'}, sort_keys=True)
    return hashlib.sha256(specification.encode()).hexdigest()[:20]


def wait_job(job, max_seconds=45 * 60):
    started = time.monotonic()
    while True:
        progress, status, _ = client.progress(job['id'])
        job.update(status=status, progress=int(progress), checked_utc=now())
        save_manifest()
        print(f"Job {job['id']}: {status}, {progress}%   ", end='\r', flush=True)
        if status == 'successful':
            print()
            return
        if status in {'failed', 'canceled', 'paused', 'complete_with_errors'}:
            print()
            raise RuntimeError(f"Job em estado {status}. ID preservado no manifesto; revise no Harmony antes de reenviar.")
        if time.monotonic() - started > max_seconds:
            print()
            raise RuntimeError('Tempo local de acompanhamento terminou. Execute novamente para retomar o mesmo job; ele não foi cancelado.')
        time.sleep(10)


def run_job(date, layers):
    from harmony import Request, Collection, BBox
    if not set(layers).issubset(VARIABLES):
        raise ValueError('Camada desconhecida.')
    key = key_for(date, layers)
    job = manifest['jobs'].get(key)
    if job is None:
        request = Request(
            Collection(id=COLLECTION_ID), spatial=BBox(*BBOX),
            variables=[VARIABLES[name] for name in layers], granule_name=[SCENES[date]],
            format='image/tiff', max_results=1, ignore_errors=False,
        )
        if not request.is_valid():
            raise RuntimeError('Requisição inválida: ' + str(request.error_messages()))
        job_id = client.submit(request)
        job = {'id': str(job_id), 'date_utc': date, 'scene': SCENES[date], 'layers': layers,
               'submitted_utc': now(), 'status': 'submitted', 'outputs': []}
        manifest['jobs'][key] = job
        save_manifest()
        print('Job submetido e salvo:', job['id'])
    else:
        print('Retomando job registrado:', job['id'])
    wait_job(job)
    urls = list(client.result_urls(job['id']))
    if not urls:
        raise RuntimeError('Job não retornou saídas. Verifique disponibilidade das variáveis.')
    for url in urls:
        parsed = urlsplit(url)
        suffix = Path(unquote(parsed.path)).suffix.lower()
        if parsed.scheme != 'https' or suffix not in {'.tif', '.tiff'}:
            raise RuntimeError('Job retornou saída que não é TIFF HTTPS. Download não iniciado; revisar no Harmony.')
    directory = RESULT_DIR / date / key
    directory.mkdir(parents=True, exist_ok=True)
    infos = []
    for url in urls:
        try:
            path = Path(client.download(url, directory=str(directory), overwrite=True).result())
        except Exception:
            raise RuntimeError('Download da saída Harmony falhou. Job ID está salvo; reexecute para tentar novamente.') from None
        info = inspect_tiff(path, date, job['id'], layers)
        infos.append(info)
        manifest['files'] = [f for f in manifest['files'] if f['file'] != info['file']] + [info]
        job['outputs'] = [f['file'] for f in infos]
        save_manifest()
    found = {f['layer'] for f in infos}
    missing = sorted(set(layers) - found)
    job['missing_layers'] = missing
    job['outputs_inspected_utc'] = now()
    save_manifest()
    if missing:
        print('ATENÇÃO: camadas não confirmadas nas saídas:', missing)
        print('O serviço pode ignorar variáveis ausentes. Não concluir que a máscara foi obtida sem identificá-la.')
    return infos


def latest_file(date, layer):
    matches = [f for f in manifest['files'] if f['date_utc'] == date and f.get('layer') == layer
               and (RESULT_DIR / f['file']).is_file()]
    return matches[-1] if matches else None


def compare_grids(first, second):
    return (first['crs'] == second['crs'] and first['width'] == second['width']
            and first['height'] == second['height']
            and bool(np.allclose(first['transform'], second['transform'], rtol=0, atol=1e-5)))


# ------------------------------------------------------------ 6. Teste
def teste_inicial():
    first_test = run_job(primeira_data(), ['HHHH'])
    if not any(info.get('layer') == 'HHHH' for info in first_test):
        raise RuntimeError('HHHH não foi identificada. Não execute a expansão antes de revisar.')
    print('Teste inicial concluído. Revise os metadados e o tamanho acima.')
    print('Manifesto:', MANIFEST_PATH)


# ------------------------------------------------------------ 8. Expansão
def expansao():
    if latest_file(primeira_data(), 'HHHH') is None:
        raise RuntimeError('O teste inicial não está salvo. Rode antes: --etapa teste')
    for date in SCENES:
        missing = [name for name in ['HHHH', 'HVHV', 'mask'] if latest_file(date, name) is None]
        if missing:
            print('Solicitando', date, missing)
            run_job(date, missing)
        else:
            print(date, ': três camadas já salvas; sem novo pedido.')
    print('Expansão terminada.')


# ------------------------------------------------------------ 9. Verificação
def verificar():
    checks = {}
    all_complete = True
    for layer in ['HHHH', 'HVHV', 'mask']:
        found = [latest_file(date, layer) for date in SCENES]
        a, b = found[0], found[-1]
        if any(f is None for f in found):
            checks[layer] = {'complete': False, 'same_grid': None}
            all_complete = False
            print(layer, ': AUSENTE/NÃO IDENTIFICADA em pelo menos uma data.')
        else:
            same = all(compare_grids(a, f) for f in found)
            checks[layer] = {'complete': True, 'same_grid': same, 'before': a['file'], 'after': b['file']}
            print(layer, ': grades coincidentes =', same)
            if not same:
                print('  PARAR: não subtrair nem comparar pixels antes de alinhar explicitamente.')
    for date in SCENES:
        reference = latest_file(date, 'HHHH')
        for layer in ['HVHV', 'mask']:
            candidate = latest_file(date, layer)
            checks[date + '_' + layer] = {'same_grid': compare_grids(reference, candidate)
                                          if reference and candidate else None}
    manifest['grid_checks'] = checks
    manifest['all_six_layers_present'] = all_complete
    manifest['verified_utc'] = now()
    save_manifest()
    chosen = [latest_file(date, layer) for date in SCENES for layer in ['HHHH', 'HVHV', 'mask']]
    total = sum(item['bytes'] for item in chosen if item)
    print(f'Tamanho dos arquivos confirmados: {total/1024**2:.2f} MiB')
    print(f'Todas as {3*len(SCENES)} camadas presentes:', all_complete)
    print('COG declarado em todos os arquivos:',
          all(item['cog_layout_declared'] for item in chosen if item) if all_complete else 'incompleto')


# ------------------------------------------------------------ 10. ZIP
def gerar_zip():
    complete = manifest.get('all_six_layers_present', False)
    zip_path = RESULT_DIR / ('XANDAR_Harmony_recortes.zip' if complete else 'XANDAR_Harmony_INCOMPLETO.zip')
    selected = [latest_file(date, layer) for date in SCENES for layer in ['HHHH', 'HVHV', 'mask']]
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as z:
        z.write(MANIFEST_PATH, MANIFEST_PATH.name)
        if (RESULT_DIR / 'capabilities.json').exists():
            z.write(RESULT_DIR / 'capabilities.json', 'capabilities.json')
        for item in selected:
            if item:
                z.write(RESULT_DIR / item['file'], item['file'])
    print(f'Pacote: {zip_path}  ({zip_path.stat().st_size/1024**2:.2f} MiB; completo={complete})')
    return zip_path


def main():
    parser = argparse.ArgumentParser(description='Recortes NISAR GCOV via NASA Harmony (versão local).')
    parser.add_argument('--etapa', choices=['tudo', 'teste', 'expansao', 'zip'], default='tudo')
    parser.add_argument('--saida', type=Path, default=BASE_DIR / 'saida' / 'XANDAR_Harmony',
                        help='Pasta de resultados (padrão: .\\saida\\XANDAR_Harmony)')
    parser.add_argument('--sim', action='store_true', help='Não perguntar antes da expansão.')
    parser.add_argument('--config', type=Path, help='config.json gerado pela tela (app.py).')
    args = parser.parse_args()

    saida = args.saida
    if args.config:
        carregar_config(args.config.resolve())
        if saida == parser.get_default('saida'):
            saida = args.config.resolve().parent
    preparar_destino(saida.resolve())
    if args.etapa == 'zip':
        verificar()
        gerar_zip()
        return

    checar_capacidades()
    autenticar()
    try:
        if args.etapa in ('tudo', 'teste'):
            teste_inicial()
        if args.etapa == 'tudo' and not args.sim:
            resp = input('\nRevisou o teste acima? Continuar com a expansão (HHHH/HVHV/mask em todas as cenas)? [s/N] ')
            if resp.strip().lower() not in ('s', 'sim', 'y', 'yes'):
                print('Parado após o teste. Para continuar depois: python 02_harmony_recorte_gcov.py --etapa expansao')
                return
        if args.etapa in ('tudo', 'expansao'):
            expansao()
            verificar()
            gerar_zip()
    finally:
        encerrar_cliente()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit('\nInterrompido. Jobs já submetidos continuam no Harmony; reexecute para retomar.')
    except RuntimeError as e:
        sys.exit(f'\nERRO: {e}')
