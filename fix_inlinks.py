import os
import sys
import requests
import logging
import time
import pandas as pd
import datetime
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import csv
import re

# Cargar variables de entorno
load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')

SHOPIFY_STORE = os.getenv('SHOPIFY_STORE')
SHOPIFY_API_TOKEN = os.getenv('SHOPIFY_API_TOKEN')

if not SHOPIFY_STORE or not SHOPIFY_API_TOKEN:
    logging.error("Faltan variables de entorno: SHOPIFY_STORE y SHOPIFY_API_TOKEN deben estar definidas en .env.")
    sys.exit("Credenciales no configuradas.")

headers = {
    'Content-Type': 'application/json',
    'X-Shopify-Access-Token': SHOPIFY_API_TOKEN
}

DRY_RUN = False  # Cambiar a False para aplicar cambios reales
CSV_FILE = 'links.csv'
LOG_FILE = 'link_changes_log.csv'  # Archivo de log de cambios

def shopify_request(method, url, **kwargs):
    response = requests.request(method, url, headers=headers, **kwargs)
    while response.status_code == 429:
        retry_after = int(response.headers.get('Retry-After', 5))
        logging.warning(f"Límite de tasa excedido. Reintentando después de {retry_after} segundos.")
        time.sleep(retry_after)
        response = requests.request(method, url, headers=headers, **kwargs)
    return response

def get_blog_info():
    # Obtener el ID y el handle del blog
    url = f"https://{SHOPIFY_STORE}/admin/api/2023-10/blogs.json"
    response = shopify_request('GET', url)
    if response.status_code == 200:
        blogs = response.json().get('blogs', [])
        if blogs:
            blog_id = blogs[0]['id']
            blog_handle = blogs[0]['handle']
            return blog_id, blog_handle
        else:
            logging.error("No se encontraron blogs en la tienda.")
    else:
        logging.error(f"Error al obtener blogs: {response.status_code} - {response.text}")
    return None, None

def get_all_articles(blog_id):
    articles = []
    limit = 250
    page_info = None
    params = {'limit': limit}
    while True:
        url = f"https://{SHOPIFY_STORE}/admin/api/2023-10/blogs/{blog_id}/articles.json"
        if page_info:
            params['page_info'] = page_info
        response = shopify_request('GET', url, params=params)
        if response.status_code == 200:
            data = response.json()
            new_articles = data.get('articles', [])
            articles.extend(new_articles)

            link_header = response.headers.get('Link', '')
            if 'rel="next"' in link_header:
                match = re.search(r'<([^>]+)>; rel="next"', link_header)
                if match:
                    next_url = match.group(1)
                    query = urlparse(next_url).query
                    params_dict = parse_qs(query)
                    page_info = params_dict.get('page_info', [None])[0]
                else:
                    break
            else:
                break
        else:
            logging.error('Error al obtener artículos: %s', response.text)
            break
    return articles

def update_article(blog_id, article_id, article_data):
    url = f"https://{SHOPIFY_STORE}/admin/api/2023-10/blogs/{blog_id}/articles/{article_id}.json"
    payload = {
        'article': article_data
    }
    if DRY_RUN:
        logging.info(f"[Modo de prueba] Artículo ID {article_id} NO se actualizará.")
        return
    response = shopify_request('PUT', url, json=payload)
    if response.status_code == 200:
        logging.info(f"Artículo ID {article_id} actualizado exitosamente.")
    else:
        logging.error(f"Error al actualizar el artículo ID {article_id}: {response.status_code} - {response.text}")

def main():
    if not os.path.isfile(CSV_FILE):
        sys.exit(f"El archivo '{CSV_FILE}' no existe en el directorio actual.")

    # Leer el CSV con los links a reemplazar
    df = pd.read_csv(CSV_FILE)
    if 'link_broken' not in df.columns or 'link_new' not in df.columns:
        sys.exit("El archivo CSV debe tener columnas 'link_broken' y 'link_new'.")

    # Eliminar filas duplicadas basadas en link_broken y link_new
    df = df.drop_duplicates(subset=['link_broken', 'link_new'])

    # Crear un diccionario para reemplazos
    replacements = {}
    for _, row in df.iterrows():
        link_broken = row['link_broken']
        link_new = row['link_new']
        replacements[link_broken] = link_new

    blog_id, blog_handle = get_blog_info()
    if not blog_id or not blog_handle:
        sys.exit("No se pudo obtener el ID o el handle del blog.")

    articles = get_all_articles(blog_id)
    logging.info(f"Se obtuvieron {len(articles)} artículos.")

    # Abrir el archivo de log de cambios en modo append
    # Columnas: timestamp, article_url, old_link, new_link, anchor_text
    with open(LOG_FILE, 'a', newline='', encoding='utf-8') as log_csv:
        writer = csv.writer(log_csv)
        # Si el archivo está vacío, escribir el encabezado
        if os.stat(LOG_FILE).st_size == 0:
            writer.writerow(['timestamp', 'article_url', 'old_link', 'new_link', 'anchor_text'])

        for article in articles:
            article_id = article['id']
            title = article.get('title', 'Sin título')
            handle = article.get('handle')
            body_html = article.get('body_html', '')
            soup = BeautifulSoup(body_html, 'html.parser')
            links_actualizados = False

            # Construir la URL del artículo
            article_url = f"https://{SHOPIFY_STORE}/blogs/{blog_handle}/{handle}"

            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if href in replacements:
                    old_href = href
                    new_href = replacements[href]
                    a_tag['href'] = new_href
                    anchor_text = a_tag.get_text(strip=True)
                    logging.info(f"Reemplazando enlace '{old_href}' por '{new_href}' en artículo '{title}'.")
                    links_actualizados = True

                    # Registrar en el log CSV
                    timestamp = datetime.datetime.now().isoformat()
                    writer.writerow([timestamp, article_url, old_href, new_href, anchor_text])

            if links_actualizados:
                # Actualizar el artículo con el nuevo body_html
                new_body_html = str(soup)
                update_article(blog_id, article_id, {'body_html': new_body_html})
                time.sleep(0.5)  # Esperar para evitar exceder límites de tasa
            else:
                logging.info(f"No se encontraron enlaces rotos a reemplazar en el artículo '{title}'.")

if __name__ == '__main__':
    main()