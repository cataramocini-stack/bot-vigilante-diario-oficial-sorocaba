import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
from datetime import datetime
import unicodedata
import urllib3

# Desativa aviso SSL (site prefeitura pode ter certificado vencido)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== CONFIGURAÇÕES ==========
URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NOME_BUSCA = os.getenv("NOME_BUSCA")
CARGO_BUSCA = os.getenv("CARGO_BUSCA")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, NOME_BUSCA, CARGO_BUSCA]):
    raise ValueError("Variáveis de ambiente não configuradas corretamente.")

# ========== SESSION ==========
session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})
session.verify = False


# ========== FUNÇÕES ==========
def normalizar(txt):
    if not txt:
        return ""
    return unicodedata.normalize("NFKD", txt).encode("ASCII", "ignore").decode("ASCII").upper()


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
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")


def buscar_links_pdf():
    response = session.get(URL_SOROCABA, timeout=40)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')

    meses = ["JANEIRO", "FEVEREIRO", "MARCO", "ABRIL", "MAIO", "JUNHO",
             "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]

    hoje = datetime.now()
    data_string = f"{hoje.day} DE {meses[hoje.month-1]} DE {hoje.year}"
    data_string = normalizar(data_string)

    links_vistos = set()
    pdfs = []

    for a in soup.find_all('a', href=True):
        texto_link = normalizar(a.get_text())
        href = a['href']

        if '.pdf' in href.lower() and data_string in texto_link:
            link_completo = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href

            if link_completo not in links_vistos:
                pdfs.append((a.get_text().strip(), link_completo))
                links_vistos.add(link_completo)

    if not pdfs:
        primeiro_link = soup.find('a', href=lambda x: x and '.pdf' in x.lower())
        if primeiro_link:
            href = primeiro_link['href']
            link_completo = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href
            pdfs.append((primeiro_link.get_text().strip(), link_completo))

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
        pdf_res = session.get(link_pdf, timeout=60)
        pdf_res.raise_for_status()

        with pdfplumber.open(io.BytesIO(pdf_res.content)) as pdf:
            for i, pagina in enumerate(pdf.pages, 1):
                texto = pagina.extract_text()
                texto_norm = normalizar(texto)

                if not texto_norm:
                    continue

                if normalizar(NOME_BUSCA) in texto_norm:
                    resultado["nome_encontrado"] = True
                    break

                if normalizar(CARGO_BUSCA) in texto_norm:
                    resultado["paginas_cargo"].append(i)

    except Exception as e:
        resultado["erro"] = str(e)

    return resultado


def buscar_diario():
    try:
        pdfs = buscar_links_pdf()

        if not pdfs:
            enviar_telegram("⚠️ Nenhum PDF encontrado.")
            return

        relatorio = []
        relatorio.append(f"🔍 *Vigilante Sorocaba*\nArquivos analisados: {len(pdfs)}\n")

        for titulo, link_pdf in pdfs:
            resultado = analisar_pdf(titulo, link_pdf)

            if resultado["erro"]:
                relatorio.append(f"❌ Erro em {titulo}\n`{resultado['erro']}`\n")
                continue

            if resultado["nome_encontrado"]:
                relatorio.append(
                    f"🚨 *NOME ENCONTRADO!*\n"
                    f"Arquivo: `{titulo}`\n"
                    f"🔗 {link_pdf}\n"
                )
            elif resultado["paginas_cargo"]:
                paginas = ", ".join(map(str, resultado["paginas_cargo"]))
                relatorio.append(
                    f"🔔 Cargo encontrado\n"
                    f"Arquivo: `{titulo}`\n"
                    f"Páginas: {paginas}\n"
                    f"🔗 {link_pdf}\n"
                )
            else:
                relatorio.append(f"✅ Nada encontrado em `{titulo}`\n")

        relatorio.append("🏁 Varredura concluída.")

        mensagem_final = "\n".join(relatorio)

        if len(mensagem_final) > 4000:
            mensagem_final = mensagem_final[:3900] + "\n\n(Conteúdo truncado...)"

        enviar_telegram(mensagem_final)

    except Exception as e:
        enviar_telegram(f"❌ Erro geral: `{str(e)}`")


if __name__ == "__main__":
    buscar_diario()
