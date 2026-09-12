import time
from app import app, db, limpar_reservas_expiradas, FilaEmail, enviar_email_direto

def processar_fila_emails():
    emails = FilaEmail.query.limit(10).all()
    for item in emails:
        sucesso = enviar_email_direto(item.destinatario, item.assunto, item.corpo, item.reply_to)
        if sucesso:
            db.session.delete(item)
            
    if emails:  # Executa o commit apenas se houver registros processados
        db.session.commit()

if __name__ == '__main__':
    print("[WORKER] Processador ativo: limpando reservas e enviando e-mails...")
    while True:
        try:
            with app.app_context():
                limpar_reservas_expiradas()
                processar_fila_emails()
        except Exception as e:
            db.session.rollback()
            print(f"[ERRO WORKER]: {str(e)}")
        time.sleep(10)
