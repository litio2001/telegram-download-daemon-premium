#!/usr/bin/env python3
# creado a partir del repositorio de Alfonso el rubio de la juliana

import asyncio
import configparser
import logging
import os
import signal
import sys
import time
import json
import math
from datetime import datetime, timedelta
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import PeerChannel, DocumentAttributeFilename, MessageMediaWebPage, InputMediaUploadedDocument
from telethon.tl.functions.messages import SendMediaRequest
import re
import hashlib
import aiofiles
import aiofiles.os

# Configuración de logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# Directorios para descargas
DOWNLOAD_DIR = os.environ.get('DOWNLOAD_DIR', '/download')

# Directorios para descargas por chats/canales específicos
DOWNLOAD_DIR_BY_CHAT = {}
FORWARD_AS_COPY = {}
DELETE_AFTER_DOWNLOAD = {}

# Configuración predeterminada para descargas
MAX_PARALLEL_DOWNLOADS = 5  # Se ajustará dinámicamente según estado premium
PARALLEL_DOWNLOADS = 0
DOWNLOADS_IN_PROGRESS = set()

# Variables globales para estado premium
IS_PREMIUM = False
PREMIUM_INFO = None

# Variables para descargas programadas
SCHEDULED_DOWNLOADS = {}  # {message_id_str: {"time": datetime, "chat_id": id, "message_id": id}}

# Configuración para notificaciones
NOTIFICATIONS_ENABLED = os.environ.get('ENABLE_NOTIFICATIONS', 'true').lower() == 'true'
NOTIFICATION_CHAT_ID = os.environ.get('NOTIFICATION_CHAT_ID', None)

# Configuración para descargas segmentadas
SEGMENT_SIZE = 20 * 1024 * 1024  # 20MB por defecto, ajustable según necesidades
CONCURRENT_SEGMENTS = 3          # Número de segmentos a descargar en paralelo

def load_from_config_file():
    global DOWNLOAD_DIR, DOWNLOAD_DIR_BY_CHAT, DELETE_AFTER_DOWNLOAD, FORWARD_AS_COPY
    
    if os.path.isfile('/app/config.ini'):
        config = configparser.ConfigParser()
        config.read('/app/config.ini')
        
        if 'directories' in config:
            directories = config['directories']
            DOWNLOAD_DIR = directories.get('download_dir', DOWNLOAD_DIR)
        
        if 'chats' in config:
            for chat_id, directory in config['chats'].items():
                try:
                    chat_id = int(chat_id)
                    DOWNLOAD_DIR_BY_CHAT[chat_id] = directory
                except ValueError:
                    logger.warning(f"Chat ID no válido en config.ini: {chat_id}")
        
        if 'forward_as_copy' in config:
            for chat_id, value in config['forward_as_copy'].items():
                try:
                    chat_id = int(chat_id)
                    FORWARD_AS_COPY[chat_id] = value.lower() in ('true', 'yes', '1')
                except ValueError:
                    logger.warning(f"Chat ID no válido en config.ini: {chat_id}")
        
        if 'delete_after_download' in config:
            for chat_id, value in config['delete_after_download'].items():
                try:
                    chat_id = int(chat_id)
                    DELETE_AFTER_DOWNLOAD[chat_id] = value.lower() in ('true', 'yes', '1')
                except ValueError:
                    logger.warning(f"Chat ID no válido en config.ini: {chat_id}")

def get_dest_dir(chat_id):
    return DOWNLOAD_DIR_BY_CHAT.get(chat_id, DOWNLOAD_DIR)

def cleanup(signum, frame):
    logger.info("Recibida señal de terminación. Limpiando...")
    sys.exit(0)

def progress_callback(downloaded_bytes, total_bytes, filename):
    if total_bytes:
        percentage = round(downloaded_bytes / total_bytes * 100, 2)
        logger.info(f"Descargando {filename}: {percentage}% de {humanbytes(total_bytes)}")
    else:
        logger.info(f"Descargando {filename}: {humanbytes(downloaded_bytes)}")

def humanbytes(size):
    if not size:
        return "0B"
    
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    
    return f"{size:.2f} {units[i]}"

def get_filename_from_message(message):
    for attr in getattr(message.media.document, 'attributes', []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return f"unknown_{int(time.time())}"

def should_ignore_message(message):
    # Ignorar mensajes sin media
    if not hasattr(message, 'media'):
        return True
    
    # Ignorar URLs/enlaces
    if isinstance(message.media, MessageMediaWebPage):
        return True
    
    # Verificar si el mensaje tiene un documento o un archivo adjunto
    if not hasattr(message.media, 'document'):
        return True
    
    return False

async def check_premium_status(client):
    """
    Verifica si la cuenta tiene estado premium.
    
    Args:
        client: Cliente de Telethon
    
    Returns:
        bool: True si la cuenta es premium, False en caso contrario
    """
    try:
        # Obtener información completa del usuario
        full_user = await client(functions.users.GetFullUserRequest(
            id='me'
        ))
        
        # Verificar si el usuario tiene premium
        is_premium = getattr(full_user.user, 'premium', False)
        
        logger.info(f"Estado de cuenta premium: {is_premium}")
        return is_premium
    except Exception as e:
        logger.error(f"Error al verificar estado premium: {str(e)}")
        return False

async def get_account_info(client):
    """
    Obtiene información detallada sobre la cuenta, incluyendo límites premium.
    """
    try:
        # Obtener información básica del usuario
        me = await client.get_me()
        
        # Obtener información completa del usuario
        full_user = await client(functions.users.GetFullUserRequest(id='me'))
        
        # Verificar estado premium
        is_premium = getattr(full_user.user, 'premium', False)
        
        # Obtener información de la cuenta
        account_info = {
            'username': me.username,
            'phone': me.phone,
            'premium': is_premium,
            'limits': {
                'max_file_size': "4GB" if is_premium else "2GB",
                'max_parallel_downloads': 10 if is_premium else 5,
                'channels_limit': "1000 canales" if is_premium else "500 canales",
            }
        }
        
        logger.info(f"Información de cuenta: {account_info}")
        return account_info
    except Exception as e:
        logger.error(f"Error al obtener información de la cuenta: {str(e)}")
        return {'premium': False, 'limits': {'max_parallel_downloads': 5}}

async def download_file_segment(client, message, dest_dir, filename, start_byte, end_byte, segment_index, total_segments):
    """
    Descarga un segmento específico de un archivo grande.
    
    Args:
        client: Cliente de Telethon
        message: Mensaje que contiene el archivo
        dest_dir: Directorio de destino
        filename: Nombre del archivo
        start_byte: Byte de inicio del segmento
        end_byte: Byte de fin del segmento
        segment_index: Índice del segmento
        total_segments: Total de segmentos
    
    Returns:
        bool: True si el segmento se descargó correctamente, False en caso contrario
    """
    try:
        segment_filename = f"{filename}.part{segment_index}"
        full_segment_path = os.path.join(dest_dir, segment_filename)
        
        # Si el segmento ya existe y tiene el tamaño correcto, omitir
        if os.path.isfile(full_segment_path):
            actual_size = await aiofiles.os.path.getsize(full_segment_path)
            expected_size = end_byte - start_byte
            if actual_size == expected_size:
                logger.info(f"Segmento {segment_index}/{total_segments} ya existe con el tamaño correcto.")
                return True
        
        logger.info(f"Descargando segmento {segment_index}/{total_segments} de {filename} ({humanbytes(start_byte)}-{humanbytes(end_byte)})")
        
        # Descargar el segmento específico
        start_time = time.time()
        await client.download_media(
            message.media,
            full_segment_path,
            offset=start_byte,
            limit=end_byte - start_byte
        )
        
        elapsed_time = time.time() - start_time
        segment_size = end_byte - start_byte
        download_speed = segment_size / (elapsed_time * 1024 * 1024) if elapsed_time > 0 else 0  # MB/s
        
        logger.info(f"Segmento {segment_index}/{total_segments} completado en {elapsed_time:.2f}s ({download_speed:.2f} MB/s)")
        return True
    except Exception as e:
        logger.error(f"Error descargando segmento {segment_index}/{total_segments}: {str(e)}")
        return False

async def combine_segments(dest_dir, filename, total_segments):
    """
    Combina todos los segmentos en un único archivo.
    
    Args:
        dest_dir: Directorio donde están los segmentos
        filename: Nombre del archivo final
        total_segments: Número total de segmentos
    
    Returns:
        bool: True si se combinaron correctamente, False en caso contrario
    """
    try:
        full_path = os.path.join(dest_dir, filename)
        
        # Crear archivo destino
        async with aiofiles.open(full_path, 'wb') as output_file:
            # Leer cada segmento y escribirlo en el archivo final
            for i in range(1, total_segments + 1):
                segment_filename = f"{filename}.part{i}"
                segment_path = os.path.join(dest_dir, segment_filename)
                
                if not os.path.isfile(segment_path):
                    raise FileNotFoundError(f"No se encuentra el segmento {i}")
                
                # Leer y escribir por bloques para optimizar memoria
                async with aiofiles.open(segment_path, 'rb') as segment_file:
                    while True:
                        chunk = await segment_file.read(8 * 1024 * 1024)  # 8MB chunks
                        if not chunk:
                            break
                        await output_file.write(chunk)
                
                # Eliminar segmento después de combinarlo
                await aiofiles.os.remove(segment_path)
                logger.info(f"Segmento {i}/{total_segments} combinado y eliminado")
        
        logger.info(f"Archivo combinado correctamente: {filename}")
        return True
    except Exception as e:
        logger.error(f"Error combinando segmentos: {str(e)}")
        return False

async def download_large_file(client, message, dest_dir, filename, file_size):
    """
    Descarga un archivo grande utilizando segmentos paralelos.
    
    Args:
        client: Cliente de Telethon
        message: Mensaje que contiene el archivo
        dest_dir: Directorio de destino
        filename: Nombre del archivo
        file_size: Tamaño del archivo en bytes
    
    Returns:
        bool: True si el archivo se descargó correctamente, False en caso contrario
    """
    global SEGMENT_SIZE, CONCURRENT_SEGMENTS
    
    try:
        # Calcular número de segmentos
        total_segments = math.ceil(file_size / SEGMENT_SIZE)
        logger.info(f"Iniciando descarga segmentada de {filename} en {total_segments} segmentos")
        
        # Crear tareas para cada segmento
        segment_tasks = []
        
        # Crear grupos de segmentos para descargar en paralelo
        for i in range(1, total_segments + 1, CONCURRENT_SEGMENTS):
            concurrent_tasks = []
            
            # Crear tareas para CONCURRENT_SEGMENTS segmentos
            for j in range(CONCURRENT_SEGMENTS):
                segment_index = i + j
                if segment_index > total_segments:
                    break
                
                start_byte = (segment_index - 1) * SEGMENT_SIZE
                end_byte = min(segment_index * SEGMENT_SIZE, file_size)
                
                task = download_file_segment(
                    client, 
                    message, 
                    dest_dir, 
                    filename, 
                    start_byte, 
                    end_byte, 
                    segment_index, 
                    total_segments
                )
                concurrent_tasks.append(task)
            
            # Ejecutar grupo de segmentos concurrentes
            results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)
            
            # Verificar si hubo errores
            for j, result in enumerate(results):
                segment_index = i + j
                if segment_index > total_segments:
                    break
                
                if isinstance(result, Exception):
                    logger.error(f"Error en segmento {segment_index}: {str(result)}")
                    return False
                elif not result:
                    logger.error(f"Falló la descarga del segmento {segment_index}")
                    return False
        
        # Combinar todos los segmentos
        combine_success = await combine_segments(dest_dir, filename, total_segments)
        return combine_success
    except Exception as e:
        logger.error(f"Error en descarga segmentada: {str(e)}")
        return False

async def send_notification(client, message, success, filename, file_size, elapsed_time=None, error=None):
    """
    Envía una notificación sobre el estado de la descarga.
    
    Args:
        client: Cliente de Telethon
        message: Mensaje original
        success: Si la descarga fue exitosa
        filename: Nombre del archivo
        file_size: Tamaño del archivo
        elapsed_time: Tiempo que tomó la descarga (opcional)
        error: Error en caso de fallo (opcional)
    """
    global NOTIFICATION_CHAT_ID, NOTIFICATIONS_ENABLED
    
    if not NOTIFICATIONS_ENABLED or not NOTIFICATION_CHAT_ID:
        return
    
    try:
        # Construir mensaje de notificación
        if success:
            speed = file_size / (elapsed_time * 1024 * 1024) if elapsed_time and elapsed_time > 0 else 0
            notification_text = (
                f"✅ Descarga completada\n\n"
                f"📁 Archivo: {filename}\n"
                f"📊 Tamaño: {humanbytes(file_size)}\n"
                f"⏱️ Tiempo: {elapsed_time:.2f}s\n"
                f"🚀 Velocidad: {speed:.2f} MB/s\n\n"
                f"📥 De: {message.chat.title if hasattr(message.chat, 'title') else 'Chat privado'}"
            )
        else:
            notification_text = (
                f"❌ Error en descarga\n\n"
                f"📁 Archivo: {filename}\n"
                f"📊 Tamaño: {humanbytes(file_size)}\n"
                f"❌ Error: {error or 'Desconocido'}\n\n"
                f"📥 De: {message.chat.title if hasattr(message.chat, 'title') else 'Chat privado'}"
            )
        
        # Enviar notificación
        notification_chat_id = int(NOTIFICATION_CHAT_ID)
        await client.send_message(notification_chat_id, notification_text)
        logger.info(f"Notificación enviada a {notification_chat_id}")
    except Exception as e:
        logger.error(f"Error enviando notificación: {str(e)}")

async def schedule_download(client, message, delay_minutes):
    """
    Programa una descarga para ejecutarse después de un tiempo determinado.
    
    Args:
        client: Cliente de Telethon
        message: Mensaje con el archivo a descargar
        delay_minutes: Minutos de retraso antes de iniciar la descarga
    
    Returns:
        bool: True si se programó correctamente, False en caso contrario
    """
    global SCHEDULED_DOWNLOADS
    
    try:
        # Crear ID único para el mensaje
        message_id_str = f"{message.chat_id}_{message.id}"
        
        # Calcular tiempo de ejecución
        schedule_time = datetime.now() + timedelta(minutes=delay_minutes)
        
        # Guardar información
        SCHEDULED_DOWNLOADS[message_id_str] = {
            "time": schedule_time,
            "chat_id": message.chat_id,
            "message_id": message.id
        }
        
        filename = get_filename_from_message(message)
        logger.info(f"Descarga programada: {filename} para {schedule_time.strftime('%Y-%m-%d %H:%M:%S')}")
        return True
    except Exception as e:
        logger.error(f"Error programando descarga: {str(e)}")
        return False

async def check_scheduled_downloads(client):
    """
    Verifica y ejecuta descargas programadas que ya hayan llegado a su tiempo.
    """
    global SCHEDULED_DOWNLOADS
    
    # Copiar diccionario para evitar cambios durante iteración
    current_downloads = SCHEDULED_DOWNLOADS.copy()
    
    for message_id_str, info in current_downloads.items():
        # Verificar si ya es hora de descargar
        if datetime.now() >= info["time"]:
            try:
                # Obtener mensaje original
                chat_id = info["chat_id"]
                message_id = info["message_id"]
                
                # Obtener mensaje completo
                message = await client.get_messages(chat_id, ids=message_id)
                
                if message:
                    # Obtener directorio destino
                    dest_dir = get_dest_dir(chat_id)
                    
                    # Iniciar descarga
                    logger.info(f"Ejecutando descarga programada para mensaje {message_id_str}")
                    asyncio.create_task(download_file(client, message, dest_dir))
                
                # Eliminar de la lista de programados
                if message_id_str in SCHEDULED_DOWNLOADS:
                    del SCHEDULED_DOWNLOADS[message_id_str]
            except Exception as e:
                logger.error(f"Error al ejecutar descarga programada {message_id_str}: {str(e)}")
                
                # Si hay error, eliminar de la lista para evitar intentos repetidos
                if message_id_str in SCHEDULED_DOWNLOADS:
                    del SCHEDULED_DOWNLOADS[message_id_str]

async def download_file(client, message, dest_dir):
    global PARALLEL_DOWNLOADS, DOWNLOADS_IN_PROGRESS, IS_PREMIUM
    
    try:
        # Si el mensaje debe ser ignorado, salir
        if should_ignore_message(message):
            return False
        
        # Si el mensaje ya está en proceso de descarga, salir
        message_id_str = f"{message.chat_id}_{message.id}"
        if message_id_str in DOWNLOADS_IN_PROGRESS:
            logger.info(f"Descarga ya en progreso para mensaje {message_id_str}")
            return False
        
        # Marcar como en proceso
        DOWNLOADS_IN_PROGRESS.add(message_id_str)
        PARALLEL_DOWNLOADS += 1
        
        # Obtener el archivo y nombre
        filename = get_filename_from_message(message)
        
        # Limpiar nombre de archivo de caracteres no válidos
        filename = re.sub(r'[^\w\-_\. ]', '_', filename)
        logger.info(f"Preparando descarga de {filename}")
        
        # Verificar si el archivo ya existe
        full_path = os.path.join(dest_dir, filename)
        if os.path.isfile(full_path):
            logger.info(f"Archivo {filename} ya existe. Omitiendo descarga.")
            PARALLEL_DOWNLOADS -= 1
            DOWNLOADS_IN_PROGRESS.remove(message_id_str)
            return False
        
        # Crear directorio de destino si no existe
        os.makedirs(dest_dir, exist_ok=True)
        
        # Obtener tamaño del archivo
        media_size = message.media.document.size
        logger.info(f"Iniciando descarga de {filename} ({humanbytes(media_size)})")
        
        start_time = time.time()
        download_success = False
        
        # Para archivos extremadamente grandes, usar descarga segmentada si es premium
        if media_size > 1.5 * 1024 * 1024 * 1024 and IS_PREMIUM:  # > 1.5GB y premium
            logger.info(f"Archivo grande detectado. Usando descarga segmentada.")
            download_success = await download_large_file(client, message, dest_dir, filename, media_size)
        else:
            # Parámetros de descarga según estado premium
            download_args = {
                'progress_callback': lambda d, t: progress_callback(d, t, filename)
            }
            
            # Ajustar parámetros para cuentas premium
            if IS_PREMIUM:
                # Aumentar tamaño del buffer para descargas más rápidas
                download_args['buffer_size'] = 1 * 1024 * 1024  # 1MB buffer para premium
                download_args['timeout'] = 3600  # 1 hora para archivos grandes
            else:
                download_args['buffer_size'] = 512 * 1024  # 512KB buffer estándar
                download_args['timeout'] = 1800  # 30 minutos
            
            # Descarga normal
            await client.download_media(message.media, full_path, **download_args)
            download_success = True
        
        elapsed_time = time.time() - start_time
        
        if download_success:
            download_speed = media_size / (elapsed_time * 1024 * 1024) if elapsed_time > 0 else 0  # MB/s
            logger.info(f"Descarga completada: {filename} en {elapsed_time:.2f}s ({download_speed:.2f} MB/s)")
            
            # Enviar notificación de éxito
            await send_notification(
                client, 
                message, 
                True, 
                filename, 
                media_size, 
                elapsed_time
            )
            
            # Manejar acciones después de la descarga según configuración
            chat_id = message.chat_id
            
            # Eliminar mensaje después de descargar si está configurado
            if DELETE_AFTER_DOWNLOAD.get(chat_id, False):
                await client.delete_messages(chat_id, [message.id])
                logger.info(f"Mensaje eliminado después de la descarga: {message_id_str}")
            
            # Reenviar como copia si está configurado
            if FORWARD_AS_COPY.get(chat_id, False):
                # Implementar lógica de reenvío aquí
                pass
        else:
            logger.error(f"Falló la descarga de {filename}")
            
            # Enviar notificación de error
            await send_notification(
                client, 
                message, 
                False, 
                filename, 
                media_size, 
                error="Falló el proceso de descarga"
            )
        
        return download_success
    except Exception as e:
        logger.error(f"Error descargando archivo: {str(e)}")
        
        # Enviar notificación de error
        if 'filename' in locals() and 'media_size' in locals():
            await send_notification(
                client, 
                message, 
                False, 
                filename, 
                media_size, 
                error=str(e)
            )
        
        return False
    finally:
        # Limpieza final
        PARALLEL_DOWNLOADS -= 1
        if message_id_str in DOWNLOADS_IN_PROGRESS:
            DOWNLOADS_IN_PROGRESS.remove(message_id_str)

async def worker(client):
    """
    Maneja la cola de descargas y mantiene un límite de descargas paralelas según estado premium.
    También verifica las descargas programadas.
    """
    global MAX_PARALLEL_DOWNLOADS, PARALLEL_DOWNLOADS, IS_PREMIUM
    
    # Establecer límite según estado premium
    MAX_PARALLEL_DOWNLOADS = 10 if IS_PREMIUM else 5
    
    logger.info(f"Iniciando worker con límite de {MAX_PARALLEL_DOWNLOADS} descargas paralelas (Premium: {IS_PREMIUM})")
    
    while True:
        try:
            # Verificar descargas programadas
            await check_scheduled_downloads(client)
            
            # Lógica para controlar descargas paralelas
            if PARALLEL_DOWNLOADS >= MAX_PARALLEL_DOWNLOADS:
                await asyncio.sleep(1)
                continue
            
            # Esperar para mantener el bucle activo pero sin consumir recursos
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error en worker: {str(e)}")
            await asyncio.sleep(30)  # Esperar más tiempo si hay error

async def main():
    global IS_PREMIUM, PREMIUM_INFO, MAX_PARALLEL_DOWNLOADS, NOTIFICATION_CHAT_ID, SEGMENT_SIZE, CONCURRENT_SEGMENTS
    
    # Manejar señales de terminación
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    
    # Cargar configuración
    load_from_config_file()
    
    # Cargar configuración adicional desde variables de entorno
    NOTIFICATION_CHAT_ID = os.environ.get('NOTIFICATION_CHAT_ID', NOTIFICATION_CHAT_ID)
    SEGMENT_SIZE_MB = int(os.environ.get('SEGMENT_SIZE_MB', 20))
    SEGMENT_SIZE = SEGMENT_SIZE_MB * 1024 * 1024  # Convertir a bytes
    CONCURRENT_SEGMENTS = int(os.environ.get('CONCURRENT_SEGMENTS', 3))
    
    # Credenciales de API
    api_id = int(os.environ.get('API_ID', 0))
    api_hash = os.environ.get('API_HASH', '')
    bot_token = os.environ.get('BOT_TOKEN', '')
    
    # Validar credenciales
    if not api_id or not api_hash:
        logger.error("API_ID y API_HASH son obligatorios. Saliendo.")
        return
    
    # Configuración del cliente
    if bot_token:
        logger.info("Iniciando como bot")
        client = TelegramClient('telegram-download-daemon', api_id, api_hash)
        await client.start(bot_token=bot_token)
    else:
        logger.info("Iniciando como usuario")
        client = TelegramClient('telegram-download-daemon', api_id, api_hash)
        await client.start()
    
    # Verificar estado premium y obtener información de la cuenta
    IS_PREMIUM = await check_premium_status(client)
    PREMIUM_INFO = await get_account_info(client)
    
    # Ajustar configuración según estado premium
    MAX_PARALLEL_DOWNLOADS = PREMIUM_INFO['limits']['max_parallel_downloads']
    
    # Verificar configuración de notificaciones
    if NOTIFICATIONS_ENABLED and NOTIFICATION_CHAT_ID:
        logger.info(f"Notificaciones habilitadas. Chat ID: {NOTIFICATION_CHAT_ID}")
    elif NOTIFICATIONS_ENABLED:
        logger.warning("Notificaciones habilitadas pero no se ha especificado NOTIFICATION_CHAT_ID")
    else:
        logger.info("Notificaciones deshabilitadas")
    
    logger.info(f"""
    ============= TELEGRAM DOWNLOAD DAEMON PREMIUM =============
    Estado Premium: {'✅ Activo' if IS_PREMIUM else '❌ No activo'}
    Límite de descargas paralelas: {MAX_PARALLEL_DOWNLOADS}
    Tamaño máximo de archivo: {PREMIUM_INFO['limits']['max_file_size']}
    Descarga segmentada: Habilitada (Tamaño de segmento: {SEGMENT_SIZE_MB}MB, Concurrencia: {CONCURRENT_SEGMENTS})
    Directorio de descarga: {DOWNLOAD_DIR}
    Notificaciones: {'✅ Activadas' if NOTIFICATIONS_ENABLED and NOTIFICATION_CHAT_ID else '❌ Desactivadas'}
    ==========================================================""")
    
    # Iniciar worker para gestionar las descargas
    asyncio.create_task(worker(client))
    
    # Manejar mensajes nuevos
    @client.on(events.NewMessage(pattern=r'(?i)^/schedule\s+(\d+)'))
    async def schedule_handler(event):
        """Manejador para comando /schedule que programa una descarga"""
        # Obtener el mensaje al que se responde
        if not event.message.reply_to_msg_id:
            await event.respond("⚠️ Debes responder a un mensaje con archivo para programarlo.")
            return
        
        # Obtener minutos de retraso
        match = re.match(r'(?i)^/schedule\s+(\d+)', event.message.text)
        if not match:
            await event.respond("⚠️ Formato incorrecto. Usa: `/schedule minutos`")
            return
        
        delay_minutes = int(match.group(1))
        
        # Limitar a un máximo razonable (24 horas)
        if delay_minutes > 1440:
            await event.respond("⚠️ El retraso máximo permitido es de 1440 minutos (24 horas).")
            delay_minutes = 1440
        
        # Obtener mensaje original
        replied_msg = await event.get_reply_message()
        
        # Verificar que tenga un archivo
        if should_ignore_message(replied_msg):
            await event.respond("⚠️ El mensaje no contiene un archivo descargable.")
            return
        
        # Programar la descarga
        if await schedule_download(client, replied_msg, delay_minutes):
            filename = get_filename_from_message(replied_msg)
            schedule_time = (datetime.now() + timedelta(minutes=delay_minutes)).strftime("%Y-%m-%d %H:%M:%S")
            await event.respond(f"✅ Descarga programada: `{filename}` para {schedule_time}")
        else:
            await event.respond("❌ Error al programar la descarga.")

    @client.on(events.NewMessage(pattern=r'(?i)^/list_scheduled'))
    async def list_scheduled_handler(event):
        """Manejador para listar descargas programadas"""
        if not SCHEDULED_DOWNLOADS:
            await event.respond("📅 No hay descargas programadas.")
            return
        
        response = "📅 **Descargas programadas:**\n\n"
        for msg_id, info in SCHEDULED_DOWNLOADS.items():
            try:
                chat_id = info["chat_id"]
                message_id = info["message_id"]
                schedule_time = info["time"].strftime("%Y-%m-%d %H:%M:%S")
                
                # Intentar obtener información del archivo
                message = await client.get_messages(chat_id, ids=message_id)
                if message and hasattr(message, 'media') and hasattr(message.media, 'document'):
                    filename = get_filename_from_message(message)
                    file_size = humanbytes(message.media.document.size)
                    response += f"- `{filename}` ({file_size}) a las {schedule_time}\n"
                else:
                    response += f"- Mensaje {msg_id} a las {schedule_time}\n"
            except Exception as e:
                response += f"- Error obteniendo información: {msg_id} a las {schedule_time}\n"
        
        await event.respond(response)

    @client.on(events.NewMessage(pattern=r'(?i)^/cancel_scheduled\s+(\d+)_(\d+)'))
    async def cancel_scheduled_handler(event):
        """Manejador para cancelar una descarga programada"""
        match = re.match(r'(?i)^/cancel_scheduled\s+(\d+)_(\d+)', event.message.text)
        if not match:
            await event.respond("⚠️ Formato incorrecto. Usa: `/cancel_scheduled chat_id_message_id`")
            return
        
        chat_id = match.group(1)
        message_id = match.group(2)
        message_id_str = f"{chat_id}_{message_id}"
        
        if message_id_str in SCHEDULED_DOWNLOADS:
            del SCHEDULED_DOWNLOADS[message_id_str]
            await event.respond(f"✅ Descarga programada {message_id_str} cancelada.")
        else:
            await event.respond(f"⚠️ No se encontró la descarga programada {message_id_str}.")

    @client.on(events.NewMessage(pattern=r'(?i)^/status'))
    async def status_handler(event):
        """Manejador para mostrar el estado del daemon"""
        status_text = (
            f"📊 **Estado del daemon**\n\n"
            f"🔹 Premium: {'✅ Activo' if IS_PREMIUM else '❌ No activo'}\n"
            f"🔹 Descargas en progreso: {PARALLEL_DOWNLOADS}/{MAX_PARALLEL_DOWNLOADS}\n"
            f"🔹 Descargas programadas: {len(SCHEDULED_DOWNLOADS)}\n"
            f"🔹 Notificaciones: {'✅ Activadas' if NOTIFICATIONS_ENABLED else '❌ Desactivadas'}\n"
            f"🔹 Directorio principal: `{DOWNLOAD_DIR}`\n"
        )
        
        if DOWNLOADS_IN_PROGRESS:
            status_text += "\n**Descargas activas:**\n"
            for msg_id in DOWNLOADS_IN_PROGRESS:
                status_text += f"- Mensaje {msg_id}\n"
        
        await event.respond(status_text)

    @client.on(events.NewMessage(pattern=r'(?i)^/help'))
    async def help_handler(event):
        """Manejador para mostrar ayuda sobre comandos disponibles"""
        help_text = (
            "📚 **Comandos disponibles**\n\n"
            "/status - Muestra el estado actual del daemon\n"
            "/schedule N - Programa la descarga de un archivo para N minutos después (responde a un mensaje)\n"
            "/list_scheduled - Muestra las descargas programadas\n"
            "/cancel_scheduled chat_id_message_id - Cancela una descarga programada\n"
            "/help - Muestra este mensaje de ayuda\n"
        )
        await event.respond(help_text)

    @client.on(events.NewMessage())
    async def handler(event):
        # Si es un mensaje privado y no es un comando, ignorar
        if event.message.is_private and not event.message.text.startswith('/'):
            return
        
        # Si es un comando que ya manejamos arriba, ignorar
        if event.message.text.startswith(('/schedule', '/list_scheduled', '/cancel_scheduled', '/status', '/help')):
            return
        
        chat_id = event.chat_id
        dest_dir = get_dest_dir(chat_id)
        
        try:
            # Verificar si podemos iniciar una nueva descarga
            if PARALLEL_DOWNLOADS < MAX_PARALLEL_DOWNLOADS:
                # Procesar el mensaje para descarga
                asyncio.create_task(download_file(client, event.message, dest_dir))
        except Exception as e:
            logger.error(f"Error procesando mensaje: {str(e)}")
    
    # Mostrar información de configuración
    logger.info(f"Directorio de descarga: {DOWNLOAD_DIR}")
    logger.info(f"Directorios por chat: {DOWNLOAD_DIR_BY_CHAT}")
    
    # Mantener el cliente en ejecución
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())