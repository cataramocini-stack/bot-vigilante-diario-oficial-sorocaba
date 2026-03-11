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
MAX_PDFS_POR_EXEC = int(os.getenv("MAX_PDFS_POR_EXEC", "8"))      # segurança
HEARTBEAT_ALERT_H = int(os.getenv("HEARTBEAT_ALERT_H", "48"))     # horas

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, NOME_BUSCA, CARGO_BUSCA]):
    raise ValueError("Variáveis de ambiente obrigatórias não configuradas.")

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ================= SESSION COM RETRY + TIMEOUT =================

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
})

retry_strategy = Retry(
    total=5,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://",  adapter)
session.mount("https://", adapter)

# ================= UTILITÁRIOS =================

def normalizar(texto: str) -> str:
    if not texto:
        return ""
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = re.sub(r"\s+", " ", texto)
    return texto.upper().strip()


def enviar_telegram(mensagem: str, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram não configurado → mensagem ignorada")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }

    try:
        r = session.post(url, data=payload, timeout=15)
        r.raise_for_status()
        logger.info("Telegram enviado com sucesso")
        return True
    except Exception as e:
        logger.error(f"Falha ao enviar Telegram: {e}")
        return False


def atualizar_heartbeat():
    try:
        with open(ARQUIVO_HEARTBEAT, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        logger.error(f"Erro ao atualizar heartbeat: {e}")


def verificar_heartbeat():
    if not os.path.exists(ARQUIVO_HEARTBEAT):
        return

    try:
        with open(ARQUIVO_HEARTBEAT, "r", encoding="utf-8") as f:
            ultima_str = f.read().strip()
        ultima = datetime.fromisoformat(ultima_str)
        delta = datetime.now() - ultima
        if delta.total_seconds() > HEARTBEAT_ALERT_H * 3600:
            enviar_telegram(
                f"⚠️ <b>ALERTA DE INATIVIDADE</b>\n"
                f"O vigilante não atualiza há mais de {HEARTBEAT_ALERT_H}h.\n"
                f"Última execução: {ultima.strftime('%d/%m/%Y %H:%M')}"
            )
    except Exception as e:
        logger.warning(f"Erro ao verificar heartbeat: {e}")


def carregar_processados() -> dict:
    if not os.path.exists(ARQUIVO_CONTROLE):
        return {}
    processados = {}
    try:
        with open(ARQUIVO_CONTROLE, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or "|" not in linha:
                    continue
                link, hash_val = linha.split("|", 1)
                processados[link] = hash_val
    except Exception as e:
        logger.error(f"Erro ao carregar controle: {e}")
    return processados


def salvar_processado(link: str, hash_val: str):
    try:
        # Backup antes de sobrescrever
        if os.path.exists(ARQUIVO_CONTROLE):
            os.replace(ARQUIVO_CONTROLE, ARQUIVO_CONTROLE_BAK)

        processados = carregar_processados()
        processados[link] = hash_val

        with open(ARQUIVO_CONTROLE, "w", encoding="utf-8") as f:
            for l, h in sorted(processados.items()):  # ordenado → mais previsível
                f.write(f"{l}|{h}\n")
        logger.info(f"Salvou hash para {link}")
    except Exception as e:
        logger.error(f"Falha ao salvar controle: {e}")


def safe_get(url: str, timeout: int = 35, **kwargs):
    try:
        return session.get(url, timeout=timeout, **kwargs)
    except SSLError:
        logger.warning(f"SSL error em {url}. Tentando sem verificação...")
        urllib3.disable_warnings()
        return session.get(url, timeout=timeout, verify=False, **kwargs)
    except RequestException as e:
        logger.error(f"Falha na requisição {url}: {e}")
        raise


# ================= COLETA DE PDFs =================

def buscar_links_pdf() -> list:
    logger.info(f"Coletando PDFs de {URL_SOROCABA}")
    resp = safe_get(URL_SOROCABA, timeout=40)
    resp.raise_for_status()

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    pdfs = []
    vistos = set()

    # 1. Links diretos em <a>
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower():
            full_url = href if href.startswith("http") else BASE_URL.rstrip("/") + "/" + href.lstrip("/")
            if full_url not in vistos:
                titulo = (a.get_text(strip=True) or "Diário Oficial").strip()
                pdfs.append((titulo, full_url))
                vistos.add(full_url)

    # 2. Fallback regex (caso o layout mude)
    if len(pdfs) < 2:
        logger.warning("Poucos PDFs encontrados via HTML → usando regex fallback")
        matches = re.findall(r'(https?://[^\s"\']+\.pdf)', html, re.IGNORECASE)
        for link in matches:
            if link not in vistos:
                pdfs.append(("Diário Oficial (regex)", link))
                vistos.add(link)

    if not pdfs:
        raise RuntimeError("Nenhum PDF encontrado. Layout do site pode ter mudado.")

    logger.info(f"Encontrados {len(pdfs)} PDFs")
    return pdfs[:MAX_PDFS_POR_EXEC]  # limite de segurança


# ================= ANÁLISE DO PDF =================

def analisar_pdf(titulo: str, url: str, processados: dict) -> dict | None:
    resultado = {
        "titulo": titulo,
        "url": url,
        "nome_encontrado": False,
        "cargo_encontrado": False,
        "trecho": None,
        "hash": None,
        "tamanho_bytes": 0,
        "erro": None
    }

    try:
        start = time.time()
        resp = safe_get(url, timeout=90)
        resp.raise_for_status()

        conteudo = resp.content
        tamanho = len(conteudo)
        resultado["tamanho_bytes"] = tamanho

        hash_sha = hashlib.sha256(conteudo).hexdigest()
        resultado["hash"] = hash_sha

        if url in processados and processados[url] == hash_sha:
            logger.info(f"PDF inalterado ({tamanho//1024} kB) → pulando: {titulo}")
            return None

        logger.info(f"Analisando PDF novo/alterado ({tamanho//1024} kB): {titulo}")

        with pdfplumber.open(io.BytesIO(conteudo)) as pdf:
            if len(pdf.pages) == 0:
                raise ValueError("PDF vazio ou corrompido")

            for pagina in pdf.pages:
                texto = pagina.extract_text() or ""
                texto_norm = normalizar(texto)
                if not texto_norm:
                    continue

                nome_norm = normalizar(NOME_BUSCA)
                if nome_norm and nome_norm in texto_norm:
                    idx = texto_norm.find(nome_norm)
                    resultado["nome_encontrado"] = True
                    resultado["trecho"] = texto_norm[max(0, idx-140):idx+140].strip()
                    break  # prioridade no nome

                cargo_norm = normalizar(CARGO_BUSCA)
                if cargo_norm and cargo_norm in texto_norm:
                    resultado["cargo_encontrado"] = True

        logger.info(f"Análise concluída em {time.time()-start:.1f}s")
        return resultado

    except Exception as e:
        resultado["erro"] = str(e)
        return resultado


# ================= FLUXO PRINCIPAL =================

def main():
    logger.info("Iniciando Vigilante Diário Oficial - Sorocaba")
    verificar_heartbeat()

    try:
        pdfs = buscar_links_pdf()
        processados = carregar_processados()

        alertas = []
        sem_match = []

        for i, (titulo, link) in enumerate(pdfs, 1):
            logger.info(f"[{i}/{len(pdfs)}] Processando: {titulo}")
            resultado = analisar_pdf(titulo, link, processados)

            if resultado is None:
                continue

            if resultado.get("erro"):
                logger.error(f"Erro no PDF {titulo}: {resultado['erro']}")
                enviar_telegram(
                    f"❌ <b>Erro ao processar PDF</b>\n"
                    f"<b>{titulo}</b>\n"
                    f"<code>{resultado['erro'][:400]}</code>\n"
                    f"<a href=\"{link}\">Ver PDF</a>"
                )
                continue

            # Salva mesmo se deu erro (evita re-tentar sempre)
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
                    f"🔔 <b>CARGO ENCONTRADO</b>\n"
                    f"<b>{titulo}</b>\n"
                    f"<a href=\"{link}\">Abrir PDF</a>"
                )
                alertas.append(alerta)
            else:
                sem_match.append((titulo, link))

            time.sleep(1.8)  # gentileza com o servidor

        # Envia resultados
        hoje = datetime.now().strftime("%d/%m/%Y %H:%M")
        if alertas:
            enviar_telegram(
                f"🔍 <b>VIGILANTE SOROCABA</b> — {hoje}\n\n"
                + "\n\n".join(alertas)
            )
        elif sem_match:
            lista = "\n".join(f"• <a href=\"{link}\">{t}</a>" for t, link in sem_match[:10])
            enviar_telegram(
                f"📰 <b>Novos Diários analisados</b> — {hoje}\n\n"
                f"{len(sem_match)} PDF(s) sem ocorrência do nome/cargo.\n\n"
                f"{lista}"
            )
        else:
            logger.info("Nenhum PDF novo ou alterado nesta execução")

        atualizar_heartbeat()

    except Exception as e:
        logger.exception("Erro crítico na execução principal")
        enviar_telegram(
            f"❌ <b>ERRO CRÍTICO NO VIGILANTE</b>\n"
            f"<code>{str(e)[:800]}</code>\n"
            f"Execução: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        raise  # para o Actions marcar como falha


if __name__ == "__main__":
    main()
