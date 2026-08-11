import os
import re
import random
import string
import threading
import smtplib
import hmac
import hashlib
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from functools import wraps
from dotenv import load_dotenv
import mercadopago
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

# Carrega as variáveis de ambiente local (.env)
load_dotenv()

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configurações do App e Banco de Dados
# --------------------------------------------------------------------------
app.secret_key = os.environ.get('SECRET_KEY', 'chave_secreta_para_desenvolvimento')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# URL do Banco de Dados com ajuste para SSL no Render
database_url = os.environ.get('DATABASE_URL')
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if "sslmode" not in database_url and "localhost" not in database_url:
        database_url += "?sslmode=require" if "?" not in database_url else "&sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///dev.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --------------------------------------------------------------------------
# Configuração do Mercado Pago
# --------------------------------------------------------------------------
MERCADOPAGO_TOKEN = os.getenv('MP_ACCESS_TOKEN', '')
MP_WEBHOOK_SECRET = os.getenv('MP_WEBHOOK_SECRET', '')
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN) if MERCADOPAGO_TOKEN else None

# --------------------------------------------------------------------------
# Injeção de Contexto Global (Templates)
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    evento_default = {
        'titulo': 'MaréVibes Halloween 2026',
        'data_hora': datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc),
        'local': 'Barraca MaréVibes, Praia do Futuro',
        'descricao': 'A maior festa de Halloween à beira-mar de Fortaleza!'
    }
    ambiente_teste = not MERCADOPAGO_TOKEN or MERCADOPAGO_TOKEN.startswith('TEST-')
    return dict(evento=evento_default, ambiente_teste=ambiente_teste)

# --------------------------------------------------------------------------
# Modelos do Banco de Dados (ORM SQLAlchemy)
# --------------------------------------------------------------------------

class Usuario(db.Model):
    __tablename__ = 'usuarios'
    
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    cpf = db.Column(db.String(14), nullable=True)
    telefone = db.Column(db.String(20), nullable=True)
    senha_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    email_verificado = db.Column(db.Boolean, default=False)
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    ingressos = db.relationship('Ingresso', backref='comprador', lazy=True)


class Lote(db.Model):
    __tablename__ = 'lotes'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(50), nullable=False)
    preco = db.Column(db.Float, nullable=False)
    quantidade_total = db.Column(db.Integer, nullable=False)
    ativo = db.Column(db.Boolean, default=False)
    data_inicio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    ingressos = db.relationship('Ingresso', backref='lote_origem', lazy=True)


class Ingresso(db.Model):
    __tablename__ = 'ingressos'

    id = db.Column(db.Integer, primary_key=True)
    codigo_qr = db.Column(db.String(50), unique=True, nullable=False)
    evento_nome = db.Column(db.String(100), nullable=False, default="MaréVibes Halloween 2026")
    status = db.Column(db.String(20), default='valido')
    data_compra = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_uso = db.Column(db.DateTime, nullable=True)
    
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=True)
    pagamento_id = db.Column(db.String(100), nullable=True)


def inicializar_banco():
    with app.app_context():
        try:
            db.create_all()

            # Configura/Garante o usuário Administrador Principal
            email_admin = "administracao@dissonanteexperiencias.com"
            admin_user = Usuario.query.filter_by(email=email_admin).first()

            if not admin_user:
                admin_user = Usuario(
                    nome="Administrador",
                    email=email_admin,
                    cpf="00000000000",
                    telefone="85999999999",
                    senha_hash=generate_password_hash("DissonanteAdmin2026!"),
                    is_admin=True,
                    email_verificado=True
                )
                db.session.add(admin_user)
                print(f"[INICIALIZAÇÃO BANCO]: Conta {email_admin} criada como Administrador.")
            else:
                if not admin_user.is_admin or not admin_user.email_verificado:
                    admin_user.is_admin = True
                    admin_user.email_verificado = True
                    print(f"[INICIALIZAÇÃO BANCO]: Permissões de Administrador atualizadas para {email_admin}.")

            # Inicialização de lotes padrão
            lote_promo = Lote.query.filter(Lote.nome.ilike('%promocional%')).first()
            if not lote_promo:
                lote_promo = Lote(nome='Lote Promocional', preco=1.00, quantidade_total=15, ativo=True)
                db.session.add(lote_promo)
            else:
                lote_promo.preco = 1.00
                lote_promo.quantidade_total = 15

            lote_1 = Lote.query.filter(Lote.nome.ilike('%1º lote%')).first()
            if not lote_1:
                lote_1 = Lote(nome='1º Lote', preco=230.00, quantidade_total=150, ativo=False)
                db.session.add(lote_1)
            else:
                lote_1.preco = 230.00

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[ERRO BANCO DE DADOS]: Falha ao inicializar banco: {str(e)}")

# --------------------------------------------------------------------------
# Decoradores e Funções Auxiliares
# --------------------------------------------------------------------------

def cliente_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'usuario_id' not in session:
            flash('Por favor, faça login para acessar esta página.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'usuario_id' not in session or not session.get('is_admin', False):
            flash('Acesso restrito. Faça login com uma conta de Administrador.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def gerar_token_confirmacao(email):
    serializer = URLSafeTimedSerializer(app.secret_key)
    return serializer.dumps(email, salt='email-confirm-salt')

def validar_token_confirmacao(token, max_age=86400):
    serializer = URLSafeTimedSerializer(app.secret_key)
    try:
        email = serializer.loads(token, salt='email-confirm-salt', max_age=max_age)
        return email
    except (SignatureExpired, BadTimeSignature):
        return None

def enviar_email_direto(destinatario, assunto, corpo_texto, reply_to=None):
    mail_server = os.environ.get('MAIL_SERVER', 'smtp-relay.brevo.com')
    mail_port = int(os.environ.get('MAIL_PORT', 587))
    mail_use_tls = os.environ.get('MAIL_USE_TLS', 'True').lower() in ['true', '1', 't']
    mail_user = os.environ.get('MAIL_USERNAME', '')
    mail_password = os.environ.get('MAIL_PASSWORD', '')
    default_sender = os.environ.get('MAIL_DEFAULT_SENDER', 'nao-responda@dissonanteexperiencias.com')

    if not mail_user or not mail_password:
        print("[ERRO E-MAIL]: MAIL_USERNAME OU MAIL_PASSWORD NÃO DEFINIDOS NAS VARIÁVEIS DO RENDER!")
        return False

    msg = MIMEMultipart()
    msg['From'] = f"Dissonante Experiências <{default_sender}>"
    msg['To'] = destinatario
    msg['Subject'] = assunto
    if reply_to:
        msg['Reply-To'] = reply_to

    msg.attach(MIMEText(corpo_texto, 'plain', 'utf-8'))

    try:
        if mail_port == 465 and not mail_use_tls:
            server = smtplib.SMTP_SSL(mail_server, mail_port, timeout=15)
        else:
            server = smtplib.SMTP(mail_server, mail_port, timeout=15)
            if mail_use_tls:
                server.starttls()
            
        server.login(mail_user, mail_password)
        server.send_message(msg)
        server.quit()
        print(f"[E-MAIL ENVIADO COM SUCESSO]: Para {destinatario}")
        return True
    except Exception as e:
        print(f"[ERRO CRÍTICO NO ENVIO DE E-MAIL]: {str(e)}")
        traceback.print_exc()
        return False

def enviar_email_confirmacao(usuario_email, usuario_nome, token):
    try:
        link_validacao = url_for('validar_email', token=token, _external=True)
        assunto = "[Dissonante Experiências] Validação do seu E-mail"
        corpo = f"""Olá, {usuario_nome}!

Seja bem-vindo(a) à Dissonante Experiências.

Para ativar a sua conta e garantir o acesso aos seus ingressos, clique no link de confirmação abaixo:
{link_validacao}

Atenção: Este link expirará em 24 horas.

Atenciosamente,
Equipe Dissonante Experiências
"""
        thread = threading.Thread(
            target=enviar_email_direto, 
            args=(usuario_email, assunto, corpo)
        )
        thread.start()
        return True
    except Exception as e:
        print(f"[ERRO PREPARAR E-MAIL]: {str(e)}")
        traceback.print_exc()
        return False

def gerar_codigo_ingresso():
    hash_aleatorio = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    return f"DISSONANTE-HLW-{hash_aleatorio}"

def extrair_ddd_e_numero(telefone_raw):
    numeros = re.sub(r'\D', '', str(telefone_raw or ''))
    if len(numeros) >= 10:
        return numeros[:2], numeros[2:]
    return "85", numeros if numeros else "999999999"

def extrair_ref_externa(ext_ref):
    try:
        if ext_ref and "|" in ext_ref:
            partes = ext_ref.split("|")
            if len(partes) == 3:
                return int(partes[0]), int(partes[1]), int(partes[2])
    except (ValueError, TypeError):
        pass
    return None, None, None

def gerar_ingressos_para_pagamento(payment_id, user_id, lote_id, quantidade):
    existentes = Ingresso.query.filter_by(pagamento_id=str(payment_id)).count()
    if existentes == 0:
        for _ in range(quantidade):
            novo_ingresso = Ingresso(
                codigo_qr=gerar_codigo_ingresso(),
                usuario_id=user_id,
                lote_id=lote_id,
                pagamento_id=str(payment_id)
            )
            db.session.add(novo_ingresso)
        db.session.commit()
        return True
    return False

def validar_assinatura_mercadopago(req):
    """ Valida a assinatura HMAC enviada pelo Mercado Pago para prevenir falsificação """
    x_signature = req.headers.get('x-signature')
    x_request_id = req.headers.get('x-request-id')
    
    if not x_signature or not MP_WEBHOOK_SECRET:
        return True # Se a Secret não for configurada no .env, releva a verificação
        
    parts = {}
    for item in x_signature.split(','):
        if '=' in item:
            key, val = item.strip().split('=', 1)
            parts[key] = val

    ts = parts.get('ts')
    v1 = parts.get('v1')

    if not ts or not v1:
        return False

    data_id = req.args.get('data.id') or (req.get_json() or {}).get('data', {}).get('id')
    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"

    hmac_obj = hmac.new(MP_WEBHOOK_SECRET.encode('utf-8'), manifest.encode('utf-8'), hashlib.sha256)
    sha256_hash = hmac_obj.hexdigest()

    return sha256_hash == v1

# --------------------------------------------------------------------------
# Rotas Públicas e Utilitários
# --------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/quem-somos')
def quem_somos():
    return render_template('quem_somos.html')

@app.route('/evento/marevibes-halloween')
def evento_marevibes():
    lotes = Lote.query.order_by(Lote.id.asc()).all()
    lote_ativo = Lote.query.filter_by(ativo=True).first()
    return render_template('evento_marevibes.html', lote=lote_ativo, lotes=lotes)

@app.route('/termos-de-uso')
def termos_de_uso():
    return render_template('termos_de_uso.html')

@app.route('/politica-de-privacidade')
def politica_privacidade():
    return render_template('politica_privacidade.html')

@app.route('/forcar-ativacao/<email>')
def forcar_ativacao(email):
    usuario = Usuario.query.filter_by(email=email.strip().lower()).first()
    if usuario:
        usuario.email_verificado = True
        db.session.commit()
        return f"Sucesso! A conta {email} foi ativada manualmente. Agora você já pode fazer login."
    return "Usuário não encontrado.", 404

@app.route('/contato', methods=['GET', 'POST'])
def contato():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email_cliente = request.form.get('email', '').strip()
        assunto = request.form.get('assunto', '').strip()
        mensagem = request.form.get('mensagem', '').strip()

        email_empresa = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME', ''))

        if not email_empresa:
            flash('Serviço de e-mail indisponível no momento. Tente contato via WhatsApp.', 'warning')
            return redirect(url_for('contato'))

        corpo = f"""Nova mensagem de contato recebida pelo site:

Nome: {nome}
E-mail do Cliente: {email_cliente}
Assunto: {assunto}

Mensagem:
--------------------------------------------------
{mensagem}
--------------------------------------------------
"""

        thread = threading.Thread(
            target=enviar_email_direto, 
            args=(email_empresa, f"[Contato via Site] {assunto}", corpo, email_cliente)
        )
        thread.start()

        flash('Mensagem enviada com sucesso! Em breve entraremos em contato.', 'success')
        return redirect(url_for('contato'))

    return render_template('contato.html')

# --------------------------------------------------------------------------
# Autenticação, Cadastro e Perfil
# --------------------------------------------------------------------------

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        try:
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip().lower()
            cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
            telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
            senha = request.form.get('senha', '')
            confirmar = request.form.get('confirmar_senha', '')

            if not nome or not email or not senha:
                flash('Preencha todos os campos obrigatórios.', 'warning')
                return redirect(url_for('cadastro'))

            if senha != confirmar:
                flash('As senhas digitadas não coincidem.', 'danger')
                return redirect(url_for('cadastro'))

            usuario_existente = Usuario.query.filter_by(email=email).first()
            if usuario_existente:
                if not usuario_existente.email_verificado:
                    token = gerar_token_confirmacao(usuario_existente.email)
                    enviar_email_confirmacao(usuario_existente.email, usuario_existente.nome, token)
                    flash('Este e-mail já possui um cadastro pendente de validação. Reenviamos o e-mail de ativação.', 'warning')
                    return redirect(url_for('login'))
                else:
                    flash('Este e-mail já está cadastrado e verificado. Faça login para continuar.', 'info')
                    return redirect(url_for('login'))

            hash_senha = generate_password_hash(senha)
            novo_usuario = Usuario(
                nome=nome,
                email=email,
                cpf=cpf,
                telefone=telefone,
                senha_hash=hash_senha,
                email_verificado=False
            )
            
            db.session.add(novo_usuario)
            db.session.commit()

            token = gerar_token_confirmacao(novo_usuario.email)
            enviar_email_confirmacao(novo_usuario.email, novo_usuario.nome, token)

            flash('Cadastro realizado com sucesso! Enviamos um e-mail de ativação para você. Verifique sua caixa de entrada e spam.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO NO CADASTRO]: {str(e)}")
            traceback.print_exc()
            flash(f'Erro interno ao realizar cadastro: {str(e)}', 'danger')
            return redirect(url_for('cadastro'))

    return render_template('cadastro.html')

@app.route('/validar-email/<token>')
def validar_email(token):
    email = validar_token_confirmacao(token)
    if not email:
        return render_template('email_confirmado.html', sucesso=False, mensagem="O link de validação é inválido ou expirou.")

    usuario = Usuario.query.filter_by(email=email).first()
    if not usuario:
        return render_template('email_confirmado.html', sucesso=False, mensagem="Usuário não encontrado.")

    if usuario.email_verificado:
        return render_template('email_confirmado.html', sucesso=True, mensagem="Seu e-mail já foi validado anteriormente!", usuario=usuario)

    usuario.email_verificado = True
    db.session.commit()

    return render_template('email_confirmado.html', sucesso=True, mensagem="E-mail verificado com sucesso!", usuario=usuario)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha', '')

        usuario = Usuario.query.filter_by(email=email).first()

        if usuario and check_password_hash(usuario.senha_hash, senha):
            if not usuario.email_verificado:
                token = gerar_token_confirmacao(usuario.email)
                enviar_email_confirmacao(usuario.email, usuario.nome, token)
                flash('Sua conta ainda não foi ativada. Reenviamos o link de confirmação para o seu e-mail.', 'warning')
                return redirect(url_for('login'))

            session.permanent = True
            session['usuario_id'] = usuario.id
            session['usuario_nome'] = usuario.nome
            session['usuario_email'] = usuario.email
            session['is_admin'] = usuario.is_admin

            if usuario.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('perfil'))
        else:
            flash('E-mail ou senha incorretos.', 'danger')

    return render_template('login.html')

@app.route('/perfil')
@cliente_required
def perfil():
    usuario = Usuario.query.get(session['usuario_id'])
    return render_template('perfil.html', usuario=usuario)

@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão encerrada com sucesso.', 'info')
    return redirect(url_for('login'))

@app.route('/editar-perfil', methods=['GET', 'POST'])
@cliente_required
def editar_perfil():
    usuario = Usuario.query.get(session['usuario_id'])
    
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
        telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
        
        if not nome:
            flash('O campo Nome é obrigatório.', 'warning')
            return redirect(url_for('editar_perfil'))

        usuario.nome = nome
        usuario.cpf = cpf
        usuario.telefone = telefone
        
        db.session.commit()
        session['usuario_nome'] = usuario.nome
        
        flash('Perfil atualizado com sucesso!', 'success')
        return redirect(url_for('perfil'))

    return render_template('editar_perfil.html', usuario=usuario)

# --------------------------------------------------------------------------
# Checkout e Ingressos
# --------------------------------------------------------------------------

@app.route('/checkout', methods=['GET', 'POST'])
@cliente_required  # Bloqueia o acesso direto de usuários não autenticados
def checkout():
    # Garante que o usuário logado existe no banco
    usuario_id = session.get('usuario_id')
    usuario_atual = Usuario.query.get(usuario_id)

    if not usuario_atual:
        flash('Sua sessão expirou. Faça login novamente.', 'warning')
        return redirect(url_for('login'))

    lote_id_req = request.form.get('lote_id', type=int) or request.args.get('lote_id', type=int)
    
    if lote_id_req:
        lote_ativo = Lote.query.get(lote_id_req)
    else:
        lote_ativo = Lote.query.filter_by(ativo=True).first()

    if not lote_ativo:
        flash('Nenhum lote de ingressos disponível no momento.', 'warning')
        return redirect(url_for('evento_marevibes'))

    if request.method == 'POST':
        try:
            quantidade = max(1, int(request.form.get('quantidade', 1)))
        except (ValueError, TypeError):
            quantidade = 1

        metodo_pagamento = request.form.get('metodo_pagamento', 'pix')

        # Valida disponibilidade no lote
        ingressos_vendidos_lote = Ingresso.query.filter_by(lote_id=lote_ativo.id).count()
        disponiveis_lote = lote_ativo.quantidade_total - ingressos_vendidos_lote

        if quantidade > disponiveis_lote:
            flash(f'Restam apenas {disponiveis_lote} ingresso(s) no {lote_ativo.nome}.', 'danger')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

        total = quantidade * lote_ativo.preco

        # Formatação dos dados do pagador usando o perfil autenticado
        nome_completo = (usuario_atual.nome or 'Cliente').strip().split(' ', 1)
        first_name = nome_completo[0]
        last_name = nome_completo[1] if len(nome_completo) > 1 else "Silva"

        cpf_usuario = re.sub(r'\D', '', usuario_atual.cpf or '')
        ddd_tel, num_tel = extrair_ddd_e_numero(usuario_atual.telefone)

        payer_payload = {
            "email": usuario_atual.email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": {
                "area_code": ddd_tel,
                "number": num_tel
            },
            "identification": {
                "type": "CPF",
                "number": cpf_usuario
            }
        }

        if not sdk:
            flash('Ambiente de demonstração: Configure MP_ACCESS_TOKEN no .env para integrar ao Mercado Pago.', 'info')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

        # Pagamento via PIX
        if metodo_pagamento == 'pix':
            payment_data = {
                "transaction_amount": float(total),
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes",
                "payment_method_id": "pix",
                "external_reference": f"{usuario_atual.id}|{lote_ativo.id}|{quantidade}",
                "payer": payer_payload
            }

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status_code = payment_response.get("status")

                if status_code in [200, 201] and payment.get("status") in ["pending", "approved"]:
                    pix_info = payment.get("point_of_interaction", {}).get("transaction_data", {})
                    session['compra_atual'] = {
                        'metodo_pagamento': 'pix',
                        'payment_id': str(payment.get("id")),
                        'lote_id': lote_ativo.id,
                        'total': float(total),
                        'quantidade': quantidade,
                        'qr_code': pix_info.get("qr_code"),
                        'qr_code_base64': pix_info.get("qr_code_base64")
                    }
                    return redirect(url_for('pagamento'))
                else:
                    cause = payment.get("cause", [])
                    detalhe = cause[0].get("description") if cause else payment.get("message", "Erro desconhecido")
                    flash(f'Erro no processamento do Pix: {detalhe}', 'danger')
                    return redirect(url_for('checkout', lote_id=lote_ativo.id))

            except Exception as e:
                print("Exceção fatal ao criar PIX:", str(e))
                traceback.print_exc()
                flash('Falha de conexão com o Mercado Pago. Tente novamente.', 'danger')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

        # Pagamento via Cartão de Crédito
        elif metodo_pagamento == 'credit_card':
            token = request.form.get('token')
            installments = int(request.form.get('installments', 1))
            payment_method_id = request.form.get('payment_method_id')
            issuer_id = request.form.get('issuer_id')

            if installments > 2:
                installments = 2

            if not token or not payment_method_id:
                flash('Dados do cartão incompletos ou não tokenizados. Verifique os dados inseridos.', 'warning')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

            payment_data = {
                "transaction_amount": float(total),
                "token": token,
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes",
                "installments": installments,
                "payment_method_id": payment_method_id,
                "external_reference": f"{usuario_atual.id}|{lote_ativo.id}|{quantidade}",
                "payer": payer_payload
            }

            if issuer_id:
                payment_data["issuer_id"] = issuer_id

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status = payment.get("status")
                payment_id = payment.get("id")

                if status == "approved":
                    gerar_ingressos_para_pagamento(payment_id, usuario_atual.id, lote_ativo.id, quantidade)

                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'approved',
                        'payment_id': str(payment_id),
                        'lote_id': lote_ativo.id,
                        'total': float(total),
                        'quantidade': quantidade
                    }

                    flash('Pagamento aprovado com sucesso! Seus ingressos foram gerados.', 'success')
                    return redirect(url_for('pagamento'))
                
                elif status == "in_process":
                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'in_process',
                        'payment_id': str(payment_id),
                        'lote_id': lote_ativo.id,
                        'total': float(total),
                        'quantidade': quantidade
                    }
                    flash('Pagamento em análise pela operadora.', 'info')
                    return redirect(url_for('pagamento'))
                else:
                    status_detail = payment.get("status_detail", "Cartão recusado.")
                    flash(f'Transação não autorizada: {status_detail}.', 'danger')
                    return redirect(url_for('checkout', lote_id=lote_ativo.id))

            except Exception as e:
                print("Exceção ao processar Cartão:", str(e))
                traceback.print_exc()
                flash('Falha na comunicação com a operadora do cartão.', 'danger')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

    return render_template('checkout.html', lote=lote_ativo, usuario=usuario_atual)

# --------------------------------------------------------------------------
# Webhook do Mercado Pago
# --------------------------------------------------------------------------

@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    if not sdk:
        return jsonify({"status": "sdk_not_configured"}), 200

    if not validar_assinatura_mercadopago(request):
        print("[ERRO WEBHOOK]: Assinatura HMAC inválida. Requisição não autorizada.")
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json() or {}
    topic = data.get("type") or request.args.get("topic") or request.args.get("type")
    payment_id = data.get("data", {}).get("id") or request.args.get("id") or request.args.get("data.id")

    if topic in ["payment", "merchant_order"] and payment_id:
        try:
            payment_info = sdk.payment().get(payment_id).get("response", {})
            status = payment_info.get("status")

            if status == "approved":
                ext_ref = payment_info.get("external_reference", "")
                user_id, lote_id, qtd = extrair_ref_externa(ext_ref)
                
                comprador = None
                lote = None

                if user_id and lote_id and qtd:
                    comprador = Usuario.query.get(user_id)
                    lote = Lote.query.get(lote_id)
                    gerar_ingressos_para_pagamento(payment_id, user_id, lote_id, qtd)
                else:
                    payer_email = payment_info.get("payer", {}).get("email")
                    comprador = Usuario.query.filter_by(email=payer_email).first()
                    lote = Lote.query.filter_by(ativo=True).first()
                    qtd = 1
                    if comprador and lote:
                        gerar_ingressos_para_pagamento(payment_id, comprador.id, lote.id, 1)

                if comprador:
                    ingressos = Ingresso.query.filter_by(pagamento_id=str(payment_id)).all()
                    codigos_str = "\n".join([f"- Código: {ing.codigo_qr}" for ing in ingressos])

                    assunto_cliente = "[Dissonante Experiências] Seus ingressos estão prontos!"
                    corpo_cliente = f"""Olá, {comprador.nome}!

Seu pagamento foi confirmado com sucesso! 🎉

Detalhes do Pedido:
--------------------------------------------------
Evento: MaréVibes Halloween 2026
Lote: {lote.nome if lote else 'Geral'}
Quantidade: {qtd}
ID Transação: {payment_id}

Seus Ingressos:
{codigos_str}

Apresente os códigos acima na portaria do evento. Você também pode consultar seus ingressos a qualquer momento acessando sua conta em nosso site.

Atenciosamente,
Equipe Dissonante Experiências
"""
                    threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cliente, corpo_cliente)).start()

                    email_admin = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME', ''))
                    if email_admin:
                        assunto_admin = f"[NOVA VENDA APROVADA] ID {payment_id}"
                        corpo_admin = f"""Nova venda confirmada via Webhook!

Cliente: {comprador.nome} ({comprador.email})
Lote: {lote.nome if lote else 'Desconhecido'}
Quantidade: {qtd}
Valor Total: R$ {payment_info.get('transaction_amount', 0.0):.2f}
ID do Pagamento: {payment_id}
"""
                        threading.Thread(target=enviar_email_direto, args=(email_admin, assunto_admin, corpo_admin)).start()

            elif status in ["refunded", "charged_back", "cancelled"]:
                ingressos_para_remover = Ingresso.query.filter_by(pagamento_id=str(payment_id)).all()
                
                if ingressos_para_remover:
                    comprador_id = ingressos_para_remover[0].usuario_id
                    comprador = Usuario.query.get(comprador_id)

                    for ing in ingressos_para_remover:
                        db.session.delete(ing)
                    
                    db.session.commit()
                    print(f"[ESTORNO PROCESSADO]: Ingressos do pagamento {payment_id} removidos com sucesso.")

                    if comprador:
                        assunto_cancelamento = "[Dissonante Experiências] Cancelamento de Ingresso / Estorno de Pagamento"
                        corpo_cancelamento = f"""Olá, {comprador.nome}.

Identificamos a devolução/estorno do pagamento (ID: {payment_id}).
Os ingressos vinculados a esta compra foram cancelados e removidos da sua conta.

Se você acredita que isso foi um engano ou não solicitou o estorno, entre em contato conosco.

Atenciosamente,
Equipe Dissonante Experiências
"""
                        threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cancelamento, corpo_cancelamento)).start()

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO CRÍTICO NO WEBHOOK]: {str(e)}")
            traceback.print_exc()

    return jsonify({"status": "ok"}), 200

@app.route('/meus-ingressos')
@cliente_required
def meus_ingressos():
    ingressos_db = Ingresso.query.options(
        joinedload(Ingresso.comprador),
        joinedload(Ingresso.lote_origem)
    ).filter_by(usuario_id=session['usuario_id']).order_by(Ingresso.data_compra.desc()).all()
    
    return render_template('meus_ingressos.html', vendas=ingressos_db)

# --------------------------------------------------------------------------
# Área Administrativa
# --------------------------------------------------------------------------

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    lotes = Lote.query.all()
    lote_ativo = Lote.query.filter_by(ativo=True).first()
    
    vendidos = Ingresso.query.count()
    utilizados = Ingresso.query.filter_by(status='utilizado').count()
    
    total_disponiveis = 0
    if lote_ativo:
        ingressos_vendidos_lote_ativo = Ingresso.query.filter_by(lote_id=lote_ativo.id).count()
        total_disponiveis = max(0, lote_ativo.quantidade_total - ingressos_vendidos_lote_ativo)

    receita_total = db.session.query(func.sum(Lote.preco))\
        .join(Ingresso, Ingresso.lote_id == Lote.id)\
        .scalar() or 0.0

    stats = {
        'lote_ativo_nome': lote_ativo.nome if lote_ativo else 'Nenhum Lote Ativo',
        'lote_ativo_preco': lote_ativo.preco if lote_ativo else 0.0,
        'ingressos_vendidos': vendidos,
        'ingressos_utilizados': utilizados,
        'total_disponiveis': total_disponiveis,
        'receita_total': receita_total
    }
    
    return render_template('admin/dashboard.html', stats=stats, lotes=lotes)

@app.route('/admin/trocar-lote/<int:lote_id>')
@admin_required
def trocar_lote_ativo(lote_id):
    Lote.query.update({Lote.ativo: False})
    lote_alvo = Lote.query.get_or_404(lote_id)
    lote_alvo.ativo = True
    db.session.commit()
    flash(f'Lote ativo alterado para: {lote_alvo.nome} (R$ {lote_alvo.preco:.2f})', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/validar', methods=['GET', 'POST'])
@admin_required
def painel_validacao():
    resultado = None
    codigo_buscado = ""

    if request.method == 'POST':
        codigo_buscado = request.form.get('codigo', '').strip()
        acao = request.form.get('acao')

        ingresso = Ingresso.query.filter_by(codigo_qr=codigo_buscado).first()

        if not ingresso:
            flash('INGRESSO NÃO ENCONTRADO NO BANCO DE DADOS!', 'danger')
        else:
            if acao == 'dar_baixa':
                if ingresso.status == 'utilizado':
                    flash('ATENÇÃO: Este ingresso JÁ FOI UTILIZADO anteriormente!', 'warning')
                else:
                    ingresso.status = 'utilizado'
                    ingresso.data_uso = datetime.now(timezone.utc)
                    db.session.commit()
                    db.session.refresh(ingresso)
                    flash('ENTRADA LIBERADA! Ingresso marcado como UTILIZADO.', 'success')
            
            resultado = ingresso

    return render_template('admin/validar.html', resultado=resultado, codigo=codigo_buscado)

# --------------------------------------------------------------------------
# Inicialização do Banco de Dados
# --------------------------------------------------------------------------

inicializar_banco()

if __name__ == '__main__':
    app.run(debug=False)
