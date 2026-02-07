import requests
from bs4 import BeautifulSoup
import pdfplumber
import io
import os
import urllib3

# Desabilita avisos de SSL (necessário para o site da prefeitura)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== CONFIGURAÇÕES ==========
NOME_BUSCA = ""
CARGO_BUSCA = ""
URL_SOROCABA = "https://noticias.sorocaba.sp.gov.br/jornal/"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ========== FUNÇÃO DE NOTIFICAÇÃO ==========

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


# ========== BUSCA NO DIÁRIO OFICIAL ==========

def buscar_diario():
    """Busca o PDF mais recente do Diário Oficial e procura pelos termos"""
    
    try:
        # Headers para simular navegador
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        # 1. Acessa página do jornal
        print("🔍 Acessando site da prefeitura...")
        response = requests.get(URL_SOROCABA, timeout=40, verify=False, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 2. Busca link do PDF mais recente
        link_pdf = None
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '.pdf' in href.lower():
                # Garante URL completa
                link_pdf = href if href.startswith('http') else "https://noticias.sorocaba.sp.gov.br" + href
                break
        
        if not link_pdf:
            enviar_telegram("⚠️ *Aviso:* Nenhum PDF encontrado no site hoje.")
            return
        
        # 3. Notifica início da análise
        enviar_telegram("🔍 *Vigilante Sorocaba: Iniciando varredura...*")
        
        nome_arquivo = link_pdf.split('/')[-1]
        enviar_telegram(f"📄 *Analisando o arquivo:* `{nome_arquivo}`")
        
        # 4. Baixa o PDF
        print(f"📥 Baixando PDF: {nome_arquivo}")
        pdf_res = requests.get(link_pdf, timeout=60, verify=False, headers=headers)
        
        # 5. Analisa o conteúdo
        encontrado_nome = False
        paginas_cargo = []
        
        with pdfplumber.open(io.BytesIO(pdf_res.content)) as pdf:
            total_paginas = len(pdf.pages)
            print(f"📖 Analisando {total_paginas} páginas...")
            
            for i, pagina in enumerate(pdf.pages, 1):
                texto = pagina.extract_text()
                
                if not texto:
                    continue
                
                texto_upper = texto.upper()
                
                # Verifica se encontrou o NOME
                if NOME_BUSCA.upper() in texto_upper:
                    encontrado_nome = True
                    print(f"🚨 NOME encontrado na página {i}!")
                
                # Verifica se encontrou o CARGO
                if CARGO_BUSCA.upper() in texto_upper:
                    paginas_cargo.append(i)
        
        # 6. Envia resultado final
        if encontrado_nome:
            enviar_telegram(
                f"🚨 *URGENTE: TERMO ENCONTRADO!*\n\n"
                f"O termo *{NOME_BUSCA}* foi localizado no Diário Oficial de hoje!\n\n"
                f"🔗 {link_pdf}"
            )
            print(f"✅ ALERTA ENVIADO: {NOME_BUSCA} encontrado!")
            
        elif paginas_cargo:
            paginas = ", ".join(map(str, paginas_cargo))
            enviar_telegram(
                f"🔔 *Atenção:*\n\n"
                f"Termo *'{CARGO_BUSCA}'* encontrado nas páginas: {paginas}\n\n"
                f"*Link:* {link_pdf}"
            )
            print(f"📋 Cargo encontrado nas páginas: {paginas}")
            
        else:
            enviar_telegram(
                f"✅ *Varredura concluída*\n\n"
                f"O PDF foi lido inteiramente ({total_paginas} páginas) e nada foi encontrado para seu nome ou cargo."
            )
            print("😴 Nada encontrado hoje.")
            
    except Exception as e:
        erro_msg = f"❌ *Erro Técnico:* `{str(e)}`"
        enviar_telegram(erro_msg)
        print(f"❌ ERRO: {e}")


# ========== EXECUÇÃO ==========

if __name__ == "__main__":
    print("🤖 Vigilante Diário Oficial - Iniciando...")
    buscar_diario()
    print("✅ Execução finalizada!")
