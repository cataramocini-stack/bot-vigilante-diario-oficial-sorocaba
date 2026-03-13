import requests
from bs4 import BeautifulSoup
import fitz
import os
import io
import time
import hashlib
import logging
import unicodedata
import re
import urllib3
from datetime import datetime
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= CONFIG =================

URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"
BASE_URL = "https://noticias.sorocaba.sp.gov.br"

ARQUIVO_CONTROLE = "pdfs_processados.txt"
ARQUIVO_HEARTBEAT = "heartbeat.txt"

MAX_PDFS_POR_EXEC = int(os.getenv("MAX_PDFS_POR_EXEC","8"))
MAX_PAGES_ANALISAR = int(os.getenv("MAX_PAGES_ANALISAR","80"))
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB","25"))
THREADS = int(os.getenv("THREADS","8"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOME_BUSCA = os.getenv("NOME_BUSCA")
CARGO_BUSCA = os.getenv("CARGO_BUSCA")

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S"
)

logger = logging.getLogger()

# ================= SESSION =================

urllib3.disable_warnings()

session = requests.Session()

session.verify = False

session.headers.update({
"User-Agent":
"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
"(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
"Accept":
"text/html,application/xhtml+xml,application/xml;q=0.9,"
"image/avif,image/webp,*/*;q=0.8",
"Accept-Language":"pt-BR,pt;q=0.9,en-US;q=0.8",
"Connection":"keep-alive",
})

# ================= UTIL =================

@lru_cache(4096)
def normalizar(txt):

    if not txt:
        return ""

    txt = unicodedata.normalize("NFKD",txt)
    txt = txt.encode("ascii","ignore").decode("ascii")
    txt = re.sub(r"\s+"," ",txt)

    return txt.upper().strip()

# ================= TELEGRAM =================

def telegram(msg):

    try:

        session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id":TELEGRAM_CHAT_ID,
                "text":msg,
                "parse_mode":"HTML",
                "disable_web_page_preview":True
            },
            timeout=10
        )

    except Exception as e:

        logger.error(f"Telegram erro: {e}")

# ================= CONTROLE =================

def carregar_controle():

    registros={}
    hashes=set()

    if not os.path.exists(ARQUIVO_CONTROLE):
        return registros,hashes

    with open(ARQUIVO_CONTROLE,"r",encoding="utf-8") as f:

        for linha in f:

            linha=linha.strip()

            if not linha:
                continue

            h,size,url = linha.split("|")

            registros[url]=(h,int(size))
            hashes.add(h)

    return registros,hashes

def salvar_controle(url,h,tam):

    with open(ARQUIVO_CONTROLE,"a",encoding="utf-8") as f:

        f.write(f"{h}|{tam}|{url}\n")

# ================= COLETA =================

def coletar_pdfs():

    logger.info("Coletando PDFs")

    r=session.get(URL_SOROCABA,timeout=30)

    soup=BeautifulSoup(r.text,"html.parser")

    pdfs=[]
    vistos=set()

    for a in soup.find_all("a",href=True):

        href=a["href"]

        if ".pdf" not in href.lower():
            continue

        full=href if href.startswith("http") else BASE_URL+"/"+href.lstrip("/")

        if full in vistos:
            continue

        vistos.add(full)

        titulo=a.get_text(strip=True) or "Diário Oficial"

        pdfs.append((titulo,full))

    logger.info(f"{len(pdfs)} encontrados")

    return pdfs[:MAX_PDFS_POR_EXEC]

# ================= HEAD CHECK =================

def pdf_precisa_download(url,registros):

    try:

        r=session.head(url,timeout=20,allow_redirects=True)

        size=r.headers.get("Content-Length")

        if not size:
            return True

        size=int(size)

        if url in registros:

            if registros[url][1]==size:
                return False

        if size > MAX_PDF_MB*1024*1024:
            logger.warning("PDF muito grande ignorado")

            return False

        return True

    except:

        return True

# ================= ANALISE PDF =================

def analisar_pdf(titulo,url,registros,hashes):

    try:

        if not pdf_precisa_download(url,registros):
            logger.info("PDF igual ignorado")
            return None

        r=session.get(url,timeout=60)

        data=r.content

        tamanho=len(data)

        hash_pdf=hashlib.sha256(data).hexdigest()

        if hash_pdf in hashes:
            return None

        logger.info(f"Lendo: {titulo}")

        nome_norm=normalizar(NOME_BUSCA)
        cargo_norm=normalizar(CARGO_BUSCA)

        nome=False
        cargo=False
        trecho=None

        doc=fitz.open(stream=data,filetype="pdf")

        limite=min(len(doc),MAX_PAGES_ANALISAR)

        for i in range(limite):

            texto=doc[i].get_text("text")

            texto_norm=normalizar(texto)

            if nome_norm and nome_norm in texto_norm:

                nome=True

                idx=texto_norm.find(nome_norm)

                trecho=texto_norm[max(0,idx-120):idx+120]

                break

            if cargo_norm and cargo_norm in texto_norm:
                cargo=True

        return {
            "titulo":titulo,
            "url":url,
            "hash":hash_pdf,
            "tamanho":tamanho,
            "nome":nome,
            "cargo":cargo,
            "trecho":trecho
        }

    except Exception as e:

        return {
            "erro":str(e),
            "titulo":titulo,
            "url":url
        }

# ================= MAIN =================

def main():

    start=time.time()

    registros,hashes=carregar_controle()

    pdfs=coletar_pdfs()

    alertas=[]
    sem_match=[]

    with ThreadPoolExecutor(max_workers=THREADS) as executor:

        futures={

            executor.submit(analisar_pdf,t,l,registros,hashes):(t,l)

            for t,l in pdfs

        }

        for future in as_completed(futures):

            res=future.result()

            if not res:
                continue

            if res.get("erro"):

                telegram(
                    f"❌ <b>Erro PDF</b>\n{res['titulo']}\n<code>{res['erro']}</code>"
                )

                continue

            salvar_controle(res["url"],res["hash"],res["tamanho"])

            if res["nome"]:

                alertas.append(
                    f"🚨 <b>NOME ENCONTRADO</b>\n"
                    f"{res['titulo']}\n"
                    f"<a href=\"{res['url']}\">Abrir PDF</a>\n\n"
                    f"<code>{res['trecho']}</code>"
                )

            elif res["cargo"]:

                alertas.append(
                    f"🔔 <b>CARGO ENCONTRADO</b>\n"
                    f"{res['titulo']}\n"
                    f"<a href=\"{res['url']}\">Abrir PDF</a>"
                )

            else:

                sem_match.append((res["titulo"],res["url"]))

    if alertas:

        telegram("\n\n".join(alertas))

    elif sem_match:

        lista="\n".join(f"• <a href=\"{l}\">{t}</a>" for t,l in sem_match)

        telegram(
            f"📰 <b>Diários analisados</b>\n\n{lista}"
        )

    logger.info(f"Tempo total: {round(time.time()-start,2)}s")

# ================= RUN =================

if __name__=="__main__":
    main()
