import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
import urllib3
from datetime import datetime

# Desabilita avisos de SSL (necessário para o site da prefeitura)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== CONFIGURAÇÕES ==========
NOME_BUSCA = "GABRIEL DE OLIVEIRA"
CARGO_BUSCA = "AGENTE DE APOIO"
URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def enviar_telegram(mensagem):
    """Envia mensagem via Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=data, timeout=20)
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")

def buscar_diario():
    """Busca TODOS os PDFs recentes do Diário Oficial e procura pelos termos"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        print("🔍 Acessando site da prefeitura...")
        response = requests.get(URL_SOROCABA, timeout=40, verify=False, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Identifica a data de hoje para filtrar apenas arquivos do dia
        # Exemplo no site: "20 DE FEVEREIRO DE 2026"
        meses = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", 
                 "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]
        hoje = datetime.now()
        data_string = f"{hoje.day} DE {meses[hoje.month-1]} DE {hoje.year}"
        
        # 2. Busca todos os links de PDF que correspondam à data de hoje
        links_vistos = set()
        pdfs_para_processar = []
        
        for a in soup.find_all('a', href=True):
            texto_link = a.get_text().upper()
            href = a['href']
            
            if '.pdf' in href.lower() and data_string in texto_link:
                link_completo = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href
                if link_completo not in links_vistos:
                    pdfs_para_processar.append((texto_link.strip(), link_completo))
                    links_vistos.add(link_completo)

        if not pdfs_para_processar:
            # Se não achou nada com a data de hoje, tenta pegar ao menos o primeiro da lista (fallback)
            primeiro_link = soup.find('a', href=lambda x: x and '.pdf' in x.lower())
            if primeiro_link:
                href = primeiro_link['href']
                link_completo = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href
                pdfs_para_processar.append((primeiro_link.get_text().strip(), link_completo))
            else:
                enviar_telegram("⚠️ *Aviso:* Nenhum PDF encontrado no site.")
                return

        enviar_telegram(f"🔍 *Vigilante Sorocaba:* Encontrei {len(pdfs_para_processar)} arquivo(s) para processar.")

        # 3. Itera sobre cada PDF encontrado
        for titulo, link_pdf in pdfs_para_processar:
            nome_arquivo = link_pdf.split('/')[-1]
            enviar_telegram(f"📄 *Analisando:* `{titulo}`")
            
            pdf_res = requests.get(link_pdf, timeout=60, verify=False, headers=headers)
            encontrado_nome = False
            paginas_cargo = []
            
            with pdfplumber.open(io.BytesIO(pdf_res.content)) as pdf:
                for i, pagina in enumerate(pdf.pages, 1):
                    texto = pagina.extract_text()
                    if not texto: continue
                    
                    texto_upper = texto.upper()
                    if NOME_BUSCA.upper() in texto_upper:
                        encontrado_nome = True
                    if CARGO_BUSCA.upper() in texto_upper:
                        paginas_cargo.append(i)
            
            # 4. Relatório por arquivo
            if encontrado_nome:
                enviar_telegram(f"🚨 *URGENTE EM:* `{nome_arquivo}`\nO termo *{NOME_BUSCA}* foi localizado!\n🔗 {link_pdf}")
            elif paginas_cargo:
                paginas = ", ".join(map(str, paginas_cargo))
                enviar_telegram(f"🔔 *Atenção:* Cargo encontrado em `{nome_arquivo}` (Págs: {paginas})\n🔗 {link_pdf}")
            else:
                print(f"Nada encontrado em {nome_arquivo}")

        enviar_telegram("✅ *Varredura de todos os arquivos concluída.*")
            
    except Exception as e:
        enviar_telegram(f"❌ *Erro Técnico:* `{str(e)}`")

if __name__ == "__main__":
    buscar_diario()
