# Executar os notebooks no Python local (sem Colab)

Os dois notebooks viraram scripts:

| Notebook | Script | Faz |
|---|---|---|
| `02_Harmony_Recorte_GCOV.ipynb` | `02_harmony_recorte_gcov.py` | Pede os recortes ao NASA Harmony (precisa de token EDL) e gera `XANDAR_Harmony_recortes.zip` |
| `03_Analise_Temporal_Harmony_ZIP.ipynb` | `03_analise_temporal.py` | Confere o ZIP e calcula as diferenças em dB (sem token, sem internet) |

## 1. Preparar (uma vez) — PowerShell

Requer **Python 3.11 ou mais novo** (`python --version`).

```powershell
cd "C:\xampp\htdocs\NASA SPACE APPS\NOTEBOOKS_PY"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
```

Se o `Activate.ps1` for bloqueado: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
Nas próximas vezes basta `cd` na pasta e `.\.venv\Scripts\Activate.ps1`.

## Tela inicial (recomendado)

```powershell
pip install -r requirements.txt   # uma vez, para instalar o streamlit
streamlit run app.py
```

Abre no navegador (http://localhost:8501). Na tela:

1. **Localidade e período**: centro (`lat, lon`, pode colar do Google Maps), lado do quadrado em km e intervalo de datas. O retângulo aparece no mapa.
2. **Buscar cenas**: consulta o catálogo público da NASA (CMR) e lista as cenas da área no período. Escolha a cena ANTES e a DEPOIS (de preferência na mesma trilha e frame: a tela avisa se não estiverem).
3. **Executar**: token na barra lateral → `1 Teste` → `2 Recorte completo + ZIP` → `3 Análise temporal`. O andamento aparece na tela, e os mapas, as estatísticas e os botões de download aparecem no final.

Cada projeto fica em `saida\projetos\<nome>\` (com `config.json`, logs e resultados) e pode ser reaberto pela barra lateral. Para outra área ou outro período, use outro nome de projeto.

Os scripts também aceitam esse config na linha de comando:
`python 02_harmony_recorte_gcov.py --config saida\projetos\<nome>\config.json` e
`python 03_analise_temporal.py --config saida\projetos\<nome>\config.json`.

## 2. Baixar os recortes (script 02)

```powershell
python 02_harmony_recorte_gcov.py
```

Faz o teste (19/08, HHHH), mostra os metadados e pergunta antes de seguir para a expansão (3 camadas × 2 datas). No fim gera `saida\XANDAR_Harmony\XANDAR_Harmony_recortes.zip`.

Opções:

- `--etapa teste` → só o teste inicial
- `--etapa expansao` → expansão + verificação + ZIP
- `--etapa zip` → só verificação + ZIP (não pede token)
- `--sim` → não pergunta antes da expansão
- `--saida <pasta>` → outra pasta de resultados

Token: digitado em campo oculto. Para não digitar toda vez: `$env:EDL_TOKEN = "seu_token"` (vale só para aquela janela do PowerShell).

Se cair ou passar de 45 min, basta rodar de novo: os jobs ficam em `manifesto_harmony.json` e são retomados sem reenviar.

## 3. Análise temporal (script 03)

```powershell
python 03_analise_temporal.py
```

Acha o ZIP sozinho (na pasta do script ou em `saida\XANDAR_Harmony\`). Ou indique: `--zip C:\caminho\XANDAR_Harmony_recortes.zip`.

Resultados em `saida\xandar_mudanca\` (GeoTIFFs de Δ em dB, PNGs, manifestos) e `saida\XANDAR_analise_temporal.zip`. As duas figuras abrem automaticamente (`--nao-abrir` para desativar).

`--self-test` roda só os testes matemáticos.

## O que mudou em relação aos notebooks

- Sem `%pip`, `google.colab`, upload/download pelo navegador e `/content`: tudo fica na subpasta `saida\`.
- Caminhos no manifesto gravados com `/`, para o ZIP gerado no Windows funcionar no script 03 (e no notebook, se for usado).
- Arquivos JSON sempre gravados em UTF-8 (no Windows o padrão seria cp1252).
- Lógica científica, verificações e mensagens idênticas às do notebook.
