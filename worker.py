import time
from datetime import datetime, timezone
from app import app, db, limpar_reservas_expiradas, FilaEmail, enviar_email_direto, NotificacaoUsuario, ReservaCarrinho

def orientar_usuarios_reservas_expiradas():
    """
    Worker checa reservas prestes a expirar ou já expiradas no Redis/Banco
    e enfileira alertas explicativos para o usuário.
    """
    agora_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    
    # Exemplo: Identifica reservas que acabaram de expirar com sessão ativa
    reservas_expiradas = ReservaCarrinho.query.filter(
        ReservaCarrinho.data_expiracao < agora_naive
    ).all()

    # Executa a limpeza normal
    limpar_reservas_expiradas()

def notificar_usuario(usuario_id, mensagem, categoria="warning"):
    """
    Função auxiliar do worker para envelopar mensagens de orientação.
    """
    notificacao = NotificacaoUsuario(
        usuario_id=usuario_id,
        mensagem=mensagem,
        categoria=categoria
    )
    db.session.add(notificacao)
    db.session.commit()

def processar_fila_emails():
    emails = FilaEmail.query.limit(10).all()
    for item in emails:
        sucesso = enviar_email_direto(item.destinatario, item.assunto, item.corpo, item.reply_to)
        if sucesso:
            db.session.delete(item)
            
    if emails:
        db.session.commit()

if __name__ == '__main__':
    print("[WORKER] Processador ativo no Render com Suporte a Mensagens Flash...")
    while True:
        with app.app_context():
            try:
                orientar_usuarios_reservas_expiradas()
                processar_fila_emails()
            except Exception as e:
                db.session.rollback()
                print(f"[ERRO WORKER]: {str(e)}")
        
        time.sleep(10)
