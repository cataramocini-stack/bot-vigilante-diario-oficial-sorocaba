import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
from datetime import datetime
import unicodedata
import urllib3
import re
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ================= CONFIG =================

URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"
ARQUIVO_CONTROLE = "pdfs_processados.txt"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOME_BUSCA = os.getenv("NOME_BUSCA")
CARGO_BUSCA = os.getenv("CARGO_BUSCA")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, NOME_BUSCA, CARGO_BUSCA]):
    raise ValueError("Variáveis de ambiente não configuradas corretamente.")

# ================= LOG =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ================= SESSION COM RETRY =================

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
session.verify = False

retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)

adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ================= FUNÇÕES =================

def normalizar(txt):
    if not txt:
        return ""
    return unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII").upper()


def contem_palavra(texto, termo):
    padrao = r'\b' + re.escape(normalizar(termo)) + r'\b'
    return re.search(padrao, texto) is not None


def carregar_processados():
    if not os.path.exists(ARQUIVO_CONTROLE):
        return set()
    with open(ARQUIVO_CONTROLE, "r") as f:
        return set(l.strip() for l in f.readlines())


def salvar_processado(link):
    with open(ARQUIVO_CONTROLE, "a") as f:
        f.write(link + "\n")


def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown"
    }

    try:
        r = session.post(url, data=data, timeout=20)
        r.raise_for_status()
        logging.info("Mensagem enviada ao Telegram.")
    except Exception as e:
        logging.error(f"Erro ao enviar Telegram: {e}")


def buscar_links_pdf():
    response = session.get(URL_SOROCABA, timeout=40)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')

    pdfs = []
    links_vistos = set()

    for a in soup.find_all('a', href=True):
        href = a['href']
        if '.pdf' in href.lower():

            link_completo = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href

            if link_completo not in links_vistos:
                pdfs.append((a.get_text().strip(), link_completo))
                links_vistos.add(link_completo)

    return pdfs


def analisar_pdf(titulo, link_pdf):
    resultado = {
        "titulo": titulo,
        "link": link_pdf,
        "nome_encontrado": False,
        "paginas_cargo": [],
        "erro": None
    }

    try:
        logging.info(f"Baixando PDF: {titulo}")

        pdf_res = session.get(link_pdf, timeout=60)
        pdf_res.raise_for_status()

        with pdfplumber.open(io.BytesIO(pdf_res.content)) as pdf:
            for i, pagina in enumerate(pdf.pages, 1):
                texto = pagina.extract_text()
                texto_norm = normalizar(texto)

                if not texto_norm:
                    continue

                # Prioridade 1: Nome
                if contem_palavra(texto_norm, NOME_BUSCA):
                    resultado["nome_encontrado"] = True
                    break

                # Prioridade 2: Cargo
                if contem_palavra(texto_norm, CARGO_BUSCA):
                    resultado["paginas_cargo"].append(i)
                    break  # para leitura antecipada

    except Exception as e:
        resultado["erro"] = str(e)

    return resultado


def buscar_diario():
    try:
        pdfs = buscar_links_pdf()

        if not pdfs:
            enviar_telegram("⚠️ Nenhum PDF encontrado.")
            return

        processados = carregar_processados()
        novos_pdfs = [(t, l) for t, l in pdfs if l not in processados]

        if not novos_pdfs:
            logging.info("Nenhum PDF novo encontrado.")
            return

        relatorio = []
        relatorio.append(
            f"🔍 *Vigilante Sorocaba*\n"
            f"📅 {datetime.now().strftime('%d/%m/%Y')}\n"
            f"📂 PDFs novos encontrados: {len(novos_pdfs)}\n"
        )

        for titulo, link_pdf in novos_pdfs:

            resultado = analisar_pdf(titulo, link_pdf)

            relatorio.append(f"\n📄 `{titulo}`")

            if resultado["erro"]:
                relatorio.append(f"❌ Erro: `{resultado['erro']}`")
                continue

            if resultado["nome_encontrado"]:
                relatorio.append("🚨 *NOME ENCONTRADO!*")
                relatorio.append(f"🔗 {link_pdf}")

            elif resultado["paginas_cargo"]:
                paginas = ", ".join(map(str, resultado["paginas_cargo"]))
                relatorio.append(f"🔔 Cargo encontrado na(s) página(s): {paginas}")
                relatorio.append(f"🔗 {link_pdf}")

            else:
                relatorio.append("✅ Nada encontrado.")

            salvar_processado(link_pdf)

        relatorio.append("\n🏁 Varredura concluída.")

        mensagem_final = "\n".join(relatorio)

        if len(mensagem_final) > 4000:
            mensagem_final = mensagem_final[:3900] + "\n\n(Conteúdo truncado...)"

        enviar_telegram(mensagem_final)

    except Exception as e:
        enviar_telegram(f"❌ Erro geral: `{str(e)}`")
        logging.error(f"Erro geral: {e}")


if __name__ == "__main__":
    buscar_diario()
