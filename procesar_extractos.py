from config import DRIVE_FOLDER_ID, ai_client, drive_service
from drive_sheets import ejecutar_proceso_drive_sheets

def main():
    print("[+] Iniciando proceso MODO PRUEBA con Control por Pestaña Archivos_Procesados...")
    
    print("\n--- CONSULTANDO MODELOS DISPONIBLES EN TU API KEY ---")
    try:
        for m in ai_client.models.list():
            print("  ↳ Modelo disponible:", m.name)
    except Exception as e:
        print("  [-] No se pudo listar modelos:", e)
    print("------------------------------------------------------\n")

    subfolder_query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    subfolders = drive_service.files().list(q=subfolder_query, fields="files(id, name)", orderBy="name").execute().get('files', [])
    
    ejecutar_proceso_drive_sheets(subfolders, DRIVE_FOLDER_ID)

if __name__ == "__main__":
    main()
