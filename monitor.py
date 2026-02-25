import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
from datetime import datetime
import unicodedata
import re
import logging
import hashlib
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.exceptions import SSLError

# ================= CONFIG =================

URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"
BASE_URL = "https://noticias.sorocaba.sp.gov.br"

ARQUIVO_CONTROLE = "pdfs_processados.txt"
ARQUIVO_HEARTBEAT = "heartbeat.txt"

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

# ================= SESSION =================

session = requests.Session()

session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
})

retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)

adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


# ================= SSL INTELIGENTE =================

def safe_get(url, timeout=40):
    try:
        return session.get(url, timeout=timeout)
    except SSLError:
        logging.warning(f"SSL falhou para {url}. Tentando novamente sem verificação.")
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return session.get(url, timeout=timeout, verify=False)


# ================= UTIL =================

def normalizar(txt):
    if not txt:
        return ""
    txt = unicodedata.normalize("NFKD", txt)
    txt = txt.encode("ASCII", "ignore").decode("ASCII")
    txt = re.sub(r"\s+", " ", txt)
    return txt.upper().strip()


def enviar_telegram(mensagem):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        r = session.post(url, data=data, timeout=20)
        r.raise_for_status()
        logging.info("Mensagem enviada ao Telegram.")
    except Exception as e:
        logging.error(f"Erro ao enviar Telegram: {e}")


# ================= HEARTBEAT =================

def atualizar_heartbeat():
    with open(ARQUIVO_HEARTBEAT, "w") as f:
        f.write(datetime.now().isoformat())


def verificar_heartbeat():
    if not os.path.exists(ARQUIVO_HEARTBEAT):
        return

    try:
        with open(ARQUIVO_HEARTBEAT, "r") as f:
            ultima = datetime.fromisoformat(f.read().strip())

        diferenca = datetime.now() - ultima

        if diferenca.total_seconds() > 60 * 60 * 48:
            enviar_telegram(
                "⚠️ <b>ALERTA:</b>\nO vigilante pode não estar rodando há mais de 48h."
            )
    except:
        pass


# ================= CONTROLE HASH =================

def carregar_processados():
    if not os.path.exists(ARQUIVO_CONTROLE):
        return {}

    processados = {}
    with open(ARQUIVO_CONTROLE, "r") as f:
        for linha in f:
            linha = linha.strip()
            if "|" in linha:
                link, hash_pdf = linha.split("|")
                processados[link] = hash_pdf
    return processados


def salvar_processado(link, hash_pdf):
    processados = carregar_processados()
    processados[link] = hash_pdf

    with open(ARQUIVO_CONTROLE, "w") as f:
        for l, h in processados.items():
            f.write(f"{l}|{h}\n")


# ================= BUSCA PDFs =================

def buscar_links_pdf():
    response = safe_get(URL_SOROCABA, timeout=40)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, 'html.parser')

    pdfs = []
    vistos = set()

    # Método principal
    for a in soup.find_all('a', href=True):
        href = a['href']
        if '.pdf' in href.lower():
            link = href if href.startswith('http') else BASE_URL + href
            if link not in vistos:
                titulo = a.get_text(strip=True) or "Diário Oficial"
                pdfs.append((titulo, link))
                vistos.add(link)

    # Fallback regex
    if not pdfs:
        logging.warning("Nenhum PDF via HTML. Tentando fallback regex...")
        matches = re.findall(r'https?://[^\s"]+\.pdf', html, re.IGNORECASE)
        for link in matches:
            if link not in vistos:
                pdfs.append(("Diário Oficial (fallback)", link))
                vistos.add(link)

    if not pdfs:
        raise Exception("Nenhum PDF encontrado - possível mudança de layout.")

    return pdfs


# ================= ANALISAR PDF =================

def analisar_pdf(titulo, link_pdf, processados):
    resultado = {
        "titulo": titulo,
        "link": link_pdf,
        "nome_encontrado": False,
        "cargo_encontrado": False,
        "trecho": None,
        "hash": None,
        "erro": None
    }

    try:
        logging.info(f"Baixando PDF: {titulo}")
        response = safe_get(link_pdf, timeout=60)
        response.raise_for_status()

        hash_pdf = hashlib.md5(response.content).hexdigest()
        resultado["hash"] = hash_pdf

        if link_pdf in processados and processados[link_pdf] == hash_pdf:
            logging.info("PDF já processado e não alterado.")
            return None

        nome_norm = normalizar(NOME_BUSCA)
        cargo_norm = normalizar(CARGO_BUSCA)

        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for pagina in pdf.pages:
                texto = pagina.extract_text()
                texto_norm = normalizar(texto)

                if not texto_norm:
                    continue

                if nome_norm in texto_norm:
                    resultado["nome_encontrado"] = True
                    idx = texto_norm.find(nome_norm)
                    resultado["trecho"] = texto_norm[max(0, idx-120):idx+120]
                    break

                if cargo_norm in texto_norm:
                    resultado["cargo_encontrado"] = True

        return resultado

    except Exception as e:
        resultado["erro"] = str(e)
        return resultado


# ================= FUNÇÃO PRINCIPAL =================

def buscar_diario():

    verificar_heartbeat()

    try:
        pdfs = buscar_links_pdf()
        processados = carregar_processados()

        alertas = []

        for titulo, link in pdfs:

            resultado = analisar_pdf(titulo, link, processados)

            if resultado is None:
                continue

            if resultado["erro"]:
                logging.error(resultado["erro"])
                enviar_telegram(
                    f"❌ <b>Erro ao analisar PDF</b>\n"
                    f"<b>{titulo}</b>\n"
                    f"<code>{resultado['erro']}</code>"
                )
                continue

            salvar_processado(link, resultado["hash"])

            if resultado["nome_encontrado"]:
                alerta = (
                    f"🚨 <b>NOME ENCONTRADO!</b>\n"
                    f"<b>{titulo}</b>\n"
                    f"<a href=\"{link}\">Abrir PDF</a>\n\n"
                    f"<code>{resultado['trecho']}</code>"
                )
                alertas.append(alerta)

            elif resultado["cargo_encontrado"]:
                alerta = (
                    f"🔔 <b>Cargo encontrado</b>\n"
                    f"<b>{titulo}</b>\n"
                    f"<a href=\"{link}\">Abrir PDF</a>"
                )
                alertas.append(alerta)

        if alertas:
            mensagem = (
                f"🔍 <b>Vigilante Sorocaba</b>\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y')}\n\n"
                + "\n\n".join(alertas)
            )
            enviar_telegram(mensagem)

        atualizar_heartbeat()

    except Exception as e:
        logging.error(f"Erro geral: {e}")
        enviar_telegram(
            f"❌ <b>Erro geral no vigilante</b>\n"
            f"<code>{str(e)}</code>"
        )


if __name__ == "__main__":
    buscar_diario()
