import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
import time
from datetime import datetime
import unicodedata
import re
import logging
import hashlib
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests.exceptions import SSLError, RequestException

# ================= CONFIG =================

URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"
BASE_URL = "https://noticias.sorocaba.sp.gov.br"

ARQUIVO_CONTROLE = "pdfs_processados.txt"
ARQUIVO_HEARTBEAT = "heartbeat.txt"
ARQUIVO_CONTROLE_BAK = "pdfs_processados.bak"

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
NOME_BUSCA        = os.getenv("NOME_BUSCA")
CARGO_BUSCA       = os.getenv("CARGO_BUSCA")

MAX_PDFS_POR_EXEC = int(os.getenv("MAX_PDFS_POR_EXEC", "8"))
HEARTBEAT_ALERT_H = int(os.getenv("HEARTBEAT_ALERT_H", "48"))

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, NOME_BUSCA, CARGO_BUSCA]):
    raise ValueError("Variáveis de ambiente obrigatórias não configuradas.")

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ================= SESSION =================

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})

retry_strategy = Retry(
    total=5,
    backoff_factor=1.5,
    status_forcelist=[429,500,502,503,504],
    allowed_methods=["GET","POST"]
)

adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://",adapter)
session.mount("https://",adapter)

# ================= UTIL =================

def normalizar(texto:str)->str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii","ignore").decode("ascii")
    texto = re.sub(r"\s+"," ",texto)
    return texto.upper().strip()


def enviar_telegram(msg:str,parse_mode="HTML")->bool:
    try:
        url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        r=session.post(url,data={
            "chat_id":TELEGRAM_CHAT_ID,
            "text":msg,
            "parse_mode":parse_mode,
            "disable_web_page_preview":True
        },timeout=15)

        r.raise_for_status()
        logger.info("Telegram enviado")
        return True
    except Exception as e:
        logger.error(f"Falha telegram: {e}")
        return False


def atualizar_heartbeat():
    with open(ARQUIVO_HEARTBEAT,"w") as f:
        f.write(datetime.now().isoformat())


def verificar_heartbeat():

    if not os.path.exists(ARQUIVO_HEARTBEAT):
        return

    try:

        with open(ARQUIVO_HEARTBEAT) as f:
            ultima=datetime.fromisoformat(f.read().strip())

        delta=datetime.now()-ultima

        if delta.total_seconds()>HEARTBEAT_ALERT_H*3600:

            enviar_telegram(
                f"⚠️ <b>ALERTA DE INATIVIDADE</b>\n"
                f"Última execução: {ultima.strftime('%d/%m/%Y %H:%M')}"
            )

    except Exception as e:
        logger.warning(f"Erro heartbeat: {e}")


# ================= CONTROLE ROBUSTO =================

def carregar_processados():

    """
    Novo formato:
    hash|tamanho|url
    """

    registros={}
    hashes=set()

    if not os.path.exists(ARQUIVO_CONTROLE):
        return registros,hashes

    try:

        with open(ARQUIVO_CONTROLE,"r",encoding="utf-8") as f:

            for linha in f:

                linha=linha.strip()

                if not linha:
                    continue

                partes=linha.split("|")

                if len(partes)==3:

                    h,size,url=partes
                    registros[url]=(h,size)
                    hashes.add(h)

                elif len(partes)==2:
                    # compatibilidade antiga
                    url,h=partes
                    registros[url]=(h,"?")
                    hashes.add(h)

    except Exception as e:
        logger.error(f"Erro lendo controle: {e}")

    return registros,hashes


def salvar_processado(url,hash_val,tamanho):

    try:

        if os.path.exists(ARQUIVO_CONTROLE):
            os.replace(ARQUIVO_CONTROLE,ARQUIVO_CONTROLE_BAK)

        registros,_=carregar_processados()

        registros[url]=(hash_val,str(tamanho))

        with open(ARQUIVO_CONTROLE,"w",encoding="utf-8") as f:

            for u,(h,s) in registros.items():
                f.write(f"{h}|{s}|{u}\n")

        logger.info("Controle atualizado")

    except Exception as e:
        logger.error(f"Erro salvando controle: {e}")


# ================= HTTP =================

def safe_get(url,timeout=40):

    try:
        return session.get(url,timeout=timeout)

    except SSLError:

        urllib3.disable_warnings()

        return session.get(url,timeout=timeout,verify=False)

    except RequestException as e:
        logger.error(f"Falha HTTP {url}: {e}")
        raise


# ================= COLETA =================

def buscar_links_pdf():

    logger.info("Coletando PDFs")

    resp=safe_get(URL_SOROCABA)
    resp.raise_for_status()

    soup=BeautifulSoup(resp.text,"html.parser")

    pdfs=[]
    vistos=set()

    for a in soup.find_all("a",href=True):

        href=a["href"]

        if ".pdf" in href.lower():

            full=href if href.startswith("http") else BASE_URL+"/"+href.lstrip("/")

            if full not in vistos:

                titulo=a.get_text(strip=True) or "Diário Oficial"

                pdfs.append((titulo,full))

                vistos.add(full)

    if not pdfs:

        matches=re.findall(r'(https?://[^\s"\']+\.pdf)',resp.text,re.IGNORECASE)

        for m in matches:
            pdfs.append(("Diário Oficial",m))

    logger.info(f"{len(pdfs)} PDFs encontrados")

    return pdfs[:MAX_PDFS_POR_EXEC]


# ================= ANALISE =================

def analisar_pdf(titulo,url,registros,hashes):

    try:

        resp=safe_get(url,90)
        resp.raise_for_status()

        conteudo=resp.content
        tamanho=len(conteudo)

        hash_sha=hashlib.sha256(conteudo).hexdigest()

        # deduplicação robusta
        if hash_sha in hashes:
            logger.info("PDF já analisado (hash)")
            return None

        if url in registros:

            h_antigo,_=registros[url]

            if h_antigo==hash_sha:
                logger.info("PDF inalterado")
                return None

        logger.info(f"Analisando {titulo} ({tamanho//1024}kb)")

        nome_norm=normalizar(NOME_BUSCA)
        cargo_norm=normalizar(CARGO_BUSCA)

        nome_encontrado=False
        cargo_encontrado=False
        trecho=None

        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:

            for pagina in pdf.pages:

                texto=pagina.extract_text() or ""
                texto_norm=normalizar(texto)

                if nome_norm and nome_norm in texto_norm:

                    nome_encontrado=True

                    idx=texto_norm.find(nome_norm)

                    trecho=texto_norm[max(0,idx-140):idx+140]

                    break

                if cargo_norm and cargo_norm in texto_norm:
                    cargo_encontrado=True

        return {
            "titulo":titulo,
            "url":url,
            "hash":hash_sha,
            "tamanho":tamanho,
            "nome":nome_encontrado,
            "cargo":cargo_encontrado,
            "trecho":trecho
        }

    except Exception as e:

        return {
            "erro":str(e),
            "url":url,
            "titulo":titulo
        }


# ================= MAIN =================

def main():

    logger.info("Vigilante iniciado")

    verificar_heartbeat()

    registros,hashes=carregar_processados()

    pdfs=buscar_links_pdf()

    alertas=[]
    sem_match=[]

    for titulo,link in pdfs:

        res=analisar_pdf(titulo,link,registros,hashes)

        if res is None:
            continue

        if res.get("erro"):

            enviar_telegram(
                f"❌ <b>Erro no PDF</b>\n"
                f"{titulo}\n"
                f"<code>{res['erro']}</code>"
            )
            continue

        salvar_processado(link,res["hash"],res["tamanho"])

        if res["nome"]:

            alertas.append(
                f"🚨 <b>NOME ENCONTRADO</b>\n"
                f"{titulo}\n"
                f"<a href=\"{link}\">Abrir PDF</a>\n\n"
                f"<code>{res['trecho']}</code>"
            )

        elif res["cargo"]:

            alertas.append(
                f"🔔 <b>CARGO ENCONTRADO</b>\n"
                f"{titulo}\n"
                f"<a href=\"{link}\">Abrir PDF</a>"
            )

        else:

            sem_match.append((titulo,link))

        time.sleep(1.5)

    hoje=datetime.now().strftime("%d/%m/%Y %H:%M")

    if alertas:

        enviar_telegram(
            f"🔍 <b>VIGILANTE SOROCABA</b> — {hoje}\n\n"
            +"\n\n".join(alertas)
        )

    elif sem_match:

        lista="\n".join(f"• <a href=\"{l}\">{t}</a>" for t,l in sem_match)

        enviar_telegram(
            f"📰 <b>Diários analisados</b>\n\n{lista}"
        )

    atualizar_heartbeat()

    logger.info("Execução concluída")


if __name__=="__main__":
    main()
