import time
from app import app, limpar_reservas_expiradas

if __name__ == '__main__':
    print("[WORKER] Agendador ativo rodando rotinas de limpeza de reservas...")
    while True:
        try:
            with app.app_context():
                limpar_reservas_expiradas()
        except Exception as e:
            print(f"[ERRO WORKER]: {str(e)}")
        time.sleep(60)  # Executa a limpeza a cada 60 segundos