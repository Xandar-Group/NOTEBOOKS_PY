"""XANDAR — tela inicial (Streamlit) para recortes NISAR GCOV.

Escolha a localidade (centro + lado em km) e o intervalo de tempo, busque as
cenas disponíveis, escolha "antes" e "depois" e rode os scripts 02 e 03 sem
editar código. Cada projeto fica em .\\saida\\projetos\\<nome>\\ com seu config.json.

Executar (PowerShell, com o .venv ativo):
    streamlit run app.py
"""
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
import re
import subprocess
import sys
import time

import requests
import streamlit as st
import pydeck as pdk

BASE_DIR = Path(__file__).resolve().parent
PROJETOS_DIR = BASE_DIR / "saida" / "projetos"
SCRIPT_02 = BASE_DIR / "02_harmony_recorte_gcov.py"
SCRIPT_03 = BASE_DIR / "03_analise_temporal.py"

COLLECTION = "NISAR_L2_GCOV_PROVISIONAL_V1"
COLLECTION_ID = "C2854338529-ASF"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"

# Valores iniciais = recorte original dos notebooks.
PADRAO = {
    "nome": "nepal_ago2026",
    "coords": "28.28870, 85.52820",
    "lado_km": 10.0,
    "periodo": (date(2026, 8, 15), date(2026, 9, 5)),
}

NOME_RE = re.compile(
    r"NISAR_L2_\w+?_GCOV_(?P<ciclo>\d{3})_(?P<trilha>\d{3})_(?P<dir>[AD])_(?P<frame>\d{3})_.*?_(?P<inicio>\d{8}T\d{6})_"
)


# --------------------------------------------------------------- utilidades
def slug(texto):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", texto.strip()).strip("_")[:60]


def ler_coords(texto):
    """Aceita 'lat, lon' (como copiado do Google Maps)."""
    partes = re.split(r"[,;\s]+", texto.strip())
    if len(partes) != 2:
        raise ValueError("Use o formato: latitude, longitude  (ex.: 28.2887, 85.5282)")
    lat, lon = (float(p) for p in partes)
    if not (-85 <= lat <= 85 and -180 <= lon <= 180):
        raise ValueError("Latitude deve estar entre -85 e 85, longitude entre -180 e 180.")
    return lat, lon


def bbox_de(lat, lon, lado_km):
    """Quadrado de lado_km centrado no ponto -> (oeste, sul, leste, norte) em graus."""
    meio = lado_km / 2.0
    dlat = meio / 111.32
    dlon = meio / (111.32 * math.cos(math.radians(lat)))
    return (round(lon - dlon, 6), round(lat - dlat, 6), round(lon + dlon, 6), round(lat + dlat, 6))


def info_granule(nome):
    m = NOME_RE.search(nome)
    return m.groupdict() if m else {}


def buscar_cenas(bbox, inicio, fim):
    params = {
        "collection_concept_id": COLLECTION_ID,
        "bounding_box": ",".join(str(v) for v in bbox),
        "temporal": f"{inicio.isoformat()}T00:00:00Z,{fim.isoformat()}T23:59:59Z",
        "page_size": 200,
        "sort_key": "start_date",
    }
    r = requests.get(CMR_URL, params=params, timeout=(20, 60))
    r.raise_for_status()
    cenas = []
    for e in r.json().get("feed", {}).get("entry", []):
        nome = e.get("producer_granule_id") or e.get("title")
        inicio_utc = e.get("time_start", "")
        meta = info_granule(nome)
        cenas.append({
            "data": inicio_utc[:10],
            "hora_utc": inicio_utc[11:19],
            "trilha": meta.get("trilha", "?"),
            "frame": meta.get("frame", "?"),
            "órbita": {"A": "asc", "D": "desc"}.get(meta.get("dir"), "?"),
            "tamanho_MB": round(float(e.get("granule_size") or 0), 1),
            "granule": nome,
        })
    return cenas


def listar_projetos():
    if not PROJETOS_DIR.exists():
        return []
    return sorted(p.name for p in PROJETOS_DIR.iterdir() if (p / "config.json").is_file())


def carregar_projeto():
    nome = st.session_state.get("projeto_sel")
    if not nome or nome == "(novo)":
        return
    cfg = json.loads((PROJETOS_DIR / nome / "config.json").read_text(encoding="utf-8"))
    c = cfg["centro"]
    st.session_state.update({
        "nome": nome,
        "coords": f"{c['lat']:.5f}, {c['lon']:.5f}",
        "lado_km": float(c["lado_km"]),
        "periodo": (date.fromisoformat(cfg["periodo"]["inicio"]), date.fromisoformat(cfg["periodo"]["fim"])),
        "cenas": cfg.get("cenas_encontradas") or [
            {"data": d, "hora_utc": "", "trilha": info_granule(g).get("trilha", "?"),
             "frame": info_granule(g).get("frame", "?"), "órbita": "", "tamanho_MB": None, "granule": g}
            for d, g in sorted(cfg["scenes"].items())],
        "sel_antes": cfg["scenes"][cfg["comparacao"]["antes"]],
        "sel_depois": cfg["scenes"][cfg["comparacao"]["depois"]],
    })


def ultimas_linhas(texto, n=60):
    # O script usa '\r' para atualizar o progresso na mesma linha.
    linhas = [ln.split("\r")[-1] for ln in texto.replace("\r\n", "\n").split("\n")]
    return "\n".join(linhas[-n:])


# ------------------------------------------------------- execução em 2º plano
def iniciar(cmd, log_path, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env.update(env_extra or {})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "w", encoding="utf-8", errors="replace")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, creationflags=flags)
    st.session_state["proc"] = proc
    st.session_state["proc_log"] = str(log_path)
    st.session_state["proc_nome"] = log_path.stem


def em_execucao():
    proc = st.session_state.get("proc")
    return proc is not None and proc.poll() is None


def acompanhar():
    proc = st.session_state.get("proc")
    if proc is None:
        return
    log_path = Path(st.session_state["proc_log"])
    caixa = st.empty()
    while True:
        texto = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
        rodando = proc.poll() is None
        with caixa.container():
            if rodando:
                st.info(f"⏳ Executando: {st.session_state['proc_nome']} — pode deixar a página aberta. "
                        "Se fechar, o job continua no Harmony e é retomado ao rodar de novo.")
            elif proc.returncode == 0:
                st.success(f"✅ Concluído: {st.session_state['proc_nome']}")
            else:
                st.error(f"❌ Terminou com erro (código {proc.returncode}): {st.session_state['proc_nome']}")
            st.code(ultimas_linhas(texto) or "(aguardando saída...)", language=None)
            st.caption(f"Log completo: {log_path}")
        if not rodando:
            break
        time.sleep(2)


# ======================================================================= TELA
st.set_page_config(page_title="XANDAR — recortes NISAR", page_icon="🛰️", layout="wide")
for k, v in PADRAO.items():
    st.session_state.setdefault(k, v)
st.session_state.setdefault("cenas", [])

st.title("🛰️ XANDAR — recortes NISAR GCOV")
st.caption(f"Coleção {COLLECTION} · recorte no servidor NASA Harmony · análise de mudança em dB")

with st.sidebar:
    st.header("Projeto")
    st.selectbox("Abrir projeto existente", ["(novo)"] + listar_projetos(),
                 key="projeto_sel", on_change=carregar_projeto)
    st.text_input("Nome do projeto", key="nome",
                  help="Vira a pasta saida\\projetos\\<nome>. Use um nome novo para cada área/período.")
    st.divider()
    st.header("Token EDL")
    token_env = bool(os.environ.get("EDL_TOKEN"))
    token = st.text_input("Token (sem 'Bearer')", type="password",
                          placeholder="usando EDL_TOKEN do ambiente" if token_env else "",
                          help="Fica só na memória desta sessão; nunca é gravado em arquivo.")
    st.caption("Gere em urs.earthdata.nasa.gov → Generate Token. Só é necessário para o recorte (passos 1 e 2).")

# ------------------------------------------------------------- 1. Localidade
col_a, col_b = st.columns([1, 1.3])
with col_a:
    st.subheader("1. Localidade e período")
    st.text_input("Centro (latitude, longitude)", key="coords",
                  help="Dica: no Google Maps, clique com o botão direito no ponto e copie as coordenadas.")
    st.number_input("Lado do quadrado (km)", min_value=1.0, max_value=50.0, step=1.0, key="lado_km",
                    help="O recorte é um quadrado centrado no ponto. 10 km ≈ 4 MB por camada a 10 m.")
    st.date_input("Intervalo de tempo", key="periodo", format="DD/MM/YYYY")

    erro_local = None
    try:
        lat, lon = ler_coords(st.session_state["coords"])
        bbox = bbox_de(lat, lon, st.session_state["lado_km"])
    except ValueError as e:
        erro_local, bbox = str(e), None
    periodo = st.session_state["periodo"]
    if not (isinstance(periodo, (tuple, list)) and len(periodo) == 2):
        erro_local = erro_local or "Escolha a data inicial e a final do intervalo."
    if erro_local:
        st.error(erro_local)
    else:
        st.caption(f"Bbox (oeste, sul, leste, norte): `{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}`")

with col_b:
    if bbox:
        w, s, e, n = bbox
        camada = pdk.Layer("PolygonLayer", data=[{"poly": [[w, s], [e, s], [e, n], [w, n]]}],
                           get_polygon="poly", get_fill_color=[220, 60, 60, 50],
                           get_line_color=[200, 30, 30], line_width_min_pixels=2, stroked=True)
        zoom = max(3, min(13, 10.5 - math.log2(st.session_state["lado_km"] / 5)))
        st.pydeck_chart(pdk.Deck(layers=[camada], map_provider="carto", map_style="light",
                                 initial_view_state=pdk.ViewState(latitude=lat, longitude=lon, zoom=zoom)),
                        height=330)

# ---------------------------------------------------------------- 2. Cenas
st.subheader("2. Cenas disponíveis")
if st.button("🔎 Buscar cenas no período", disabled=bool(erro_local)):
    with st.spinner("Consultando o catálogo CMR da NASA (público, sem token)..."):
        try:
            st.session_state["cenas"] = buscar_cenas(bbox, *periodo)
        except Exception as e:
            st.session_state["cenas"] = []
            st.error(f"Falha na busca: {e}. Você pode informar as cenas manualmente abaixo.")

cenas = st.session_state["cenas"]
if cenas:
    st.dataframe(cenas, width="stretch", hide_index=True)
elif not erro_local:
    st.caption("Clique em buscar. Nenhuma cena listada ainda.")

nomes = [c["granule"] for c in cenas]
rotulo = {c["granule"]: f"{c['data']} {c['hora_utc']} · trilha {c['trilha']} · frame {c['frame']} · {c['órbita']}"
          for c in cenas}

with st.expander("Informar nomes de cenas manualmente (se a busca falhar)"):
    manual_antes = st.text_input("Granule ANTES", key="manual_antes")
    manual_depois = st.text_input("Granule DEPOIS", key="manual_depois")

c1, c2 = st.columns(2)
with c1:
    idx = nomes.index(st.session_state.get("sel_antes")) if st.session_state.get("sel_antes") in nomes else 0
    antes = st.selectbox("Cena ANTES", nomes, index=idx if nomes else None,
                         format_func=lambda g: rotulo.get(g, g)) if nomes else None
with c2:
    padrao_dep = len(nomes) - 1
    idx = nomes.index(st.session_state.get("sel_depois")) if st.session_state.get("sel_depois") in nomes else padrao_dep
    depois = st.selectbox("Cena DEPOIS", nomes, index=idx if nomes else None,
                          format_func=lambda g: rotulo.get(g, g)) if nomes else None
antes = manual_antes.strip() or antes
depois = manual_depois.strip() or depois


def data_de(g):
    for c in cenas:
        if c["granule"] == g:
            return c["data"]
    m = info_granule(g or "")
    return f"{m['inicio'][:4]}-{m['inicio'][4:6]}-{m['inicio'][6:8]}" if m else None


problemas = []
if antes and depois:
    da, dd = data_de(antes), data_de(depois)
    ia, idp = info_granule(antes), info_granule(depois)
    if not da or not dd:
        problemas.append("Não consegui identificar a data de uma das cenas (nome fora do padrão NISAR).")
    elif da >= dd:
        problemas.append("A cena ANTES precisa ser de uma data anterior à cena DEPOIS (e em dias diferentes).")
    if ia and idp and (ia["trilha"], ia["frame"], ia["dir"]) != (idp["trilha"], idp["frame"], idp["dir"]):
        problemas.append("As cenas são de trilha/frame/órbita diferentes: as grades não vão coincidir e a "
                         "análise (passo 3) vai recusar a comparação. Prefira cenas da mesma trilha e frame.")
for p in problemas:
    st.warning(p)

# ------------------------------------------------------- 3. Salvar e executar
st.subheader("3. Executar")
nome_proj = slug(st.session_state["nome"])
pasta = PROJETOS_DIR / nome_proj if nome_proj else None
cfg_path = pasta / "config.json" if pasta else None

pode_salvar = bool(nome_proj and bbox and antes and depois and not erro_local
                   and not any("ANTES precisa" in p or "identificar a data" in p for p in problemas))
novo_cfg = None
if pode_salvar:
    da, dd = data_de(antes), data_de(depois)
    novo_cfg = {
        "nome": nome_proj, "collection": COLLECTION, "collection_id": COLLECTION_ID,
        "centro": {"lat": lat, "lon": lon, "lado_km": st.session_state["lado_km"]},
        "bbox": list(bbox),
        "periodo": {"inicio": periodo[0].isoformat(), "fim": periodo[1].isoformat()},
        "scenes": {da: antes, dd: depois},
        "comparacao": {"antes": da, "depois": dd},
        "cenas_encontradas": cenas,
    }
    # Pasta já usada para outra área/cenas? Não misturar (o manifesto do Harmony recusaria).
    manifesto = pasta / "manifesto_harmony.json"
    if manifesto.exists():
        m = json.loads(manifesto.read_text(encoding="utf-8"))
        if m.get("scenes") != novo_cfg["scenes"] or tuple(m.get("bbox_wgs84", [])) != tuple(bbox):
            st.error(f"O projeto '{nome_proj}' já tem recortes de outra área/cenas. Use outro nome de projeto.")
            pode_salvar = False


def salvar_config():
    novo_cfg["salvo_utc"] = datetime.now(timezone.utc).isoformat()
    pasta.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(novo_cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def log_para(etapa):
    return pasta / "logs" / f"{etapa}_{datetime.now():%Y%m%d_%H%M%S}.log"


ocupado = em_execucao()
tem_token = bool(token.strip()) or token_env
b1, b2, b3 = st.columns(3)
with b1:
    if st.button("1️⃣ Teste (HHHH da cena ANTES)", width="stretch",
                 disabled=ocupado or not pode_salvar or not tem_token):
        salvar_config()
        iniciar([sys.executable, "-u", str(SCRIPT_02), "--config", str(cfg_path), "--etapa", "teste"],
                log_para("02_teste"), {"EDL_TOKEN": token.strip()} if token.strip() else None)
with b2:
    if st.button("2️⃣ Recorte completo + ZIP", width="stretch",
                 disabled=ocupado or not pode_salvar or not tem_token,
                 help="HHHH, HVHV e máscara nas duas cenas. Reaproveita o que já foi baixado."):
        salvar_config()
        iniciar([sys.executable, "-u", str(SCRIPT_02), "--config", str(cfg_path), "--etapa", "expansao"],
                log_para("02_expansao"), {"EDL_TOKEN": token.strip()} if token.strip() else None)
with b3:
    zip_ok = bool(pasta and (pasta / "XANDAR_Harmony_recortes.zip").exists())
    if st.button("3️⃣ Análise temporal", width="stretch",
                 disabled=ocupado or not zip_ok or not cfg_path or not cfg_path.exists(),
                 help="Disponível depois que o passo 2 gerar o ZIP completo. Não usa token."):
        iniciar([sys.executable, "-u", str(SCRIPT_03), "--config", str(cfg_path), "--nao-abrir"],
                log_para("03_analise"))

if not tem_token:
    st.caption("Informe o token EDL na barra lateral para liberar os passos 1 e 2.")
if st.session_state.get("proc") is not None:
    acompanhar()

# ------------------------------------------------------------- Resultados
if pasta and (pasta / "xandar_mudanca" / "manifesto_analise_temporal.json").exists():
    st.subheader("Resultados")
    saida = pasta / "xandar_mudanca"
    man = json.loads((saida / "manifesto_analise_temporal.json").read_text(encoding="utf-8"))
    for layer in ("HHHH", "HVHV"):
        png = saida / f"comparacao_{layer}.png"
        if png.exists():
            st.image(str(png), caption=layer, width="stretch")
        est = man.get("statistics", {}).get(layer)
        if est:
            m1, m2, m3 = st.columns(3)
            m1.metric(f"{layer} — mediana Δ", f"{est['median_db']:+.2f} dB")
            m2.metric("MAD", f"{est['mad_db']:.2f} dB")
            m3.metric("Pixels válidos", f"{est['common_valid_percent_of_aoi']:.1f}%")
    st.caption("Diferença descritiva de retroespalhamento (depois − antes). Não é deformação nem classificação; "
               "dados PROVISIONAL.")
    d1, d2 = st.columns(2)
    for col, arq in ((d1, pasta / "XANDAR_analise_temporal.zip"), (d2, pasta / "XANDAR_Harmony_recortes.zip")):
        if arq.exists():
            col.download_button(f"⬇️ {arq.name}", arq.read_bytes(), file_name=arq.name, width="stretch")
    st.caption(f"Pasta do projeto: {pasta}")
