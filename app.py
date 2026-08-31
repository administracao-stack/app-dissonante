import os
import re
import random
import string
import threading
import smtplib
import hmac
import hashlib
import traceback
import json
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from functools import wraps
from dotenv import load_dotenv
import mercadopago
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

# Carrega as variáveis de ambiente local (.env)
load_dotenv()

app = Flask(__name__)

# --------------------------------------------------------------------------
# Regra de Janela de Tempo das Vendas
# --------------------------------------------------------------------------
# Configurado para o fuso horário oficial de Brasília/Fortaleza (UTC-3)
TZ_BRASILIA = timezone(timedelta(hours=-3))
DATA_LIMITE_VENDAS = datetime(2026, 10, 31, 19, 0, 0, tzinfo=TZ_BRASILIA)

def vendas_encerradas():
    return datetime.now(timezone.utc) >= DATA_LIMITE_VENDAS

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
MP_PUBLIC_KEY = os.getenv('MP_PUBLIC_KEY', '') # Carrega a chave pública do .env
MP_WEBHOOK_SECRET = os.getenv('MP_WEBHOOK_SECRET', '')
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN) if MERCADOPAGO_TOKEN else None

# --------------------------------------------------------------------------
# Injeção de Contexto Global (Templates)
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    evento_default = {
        'titulo': 'MaréVibes Halloween 2026',
        'data_hora': datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc),
        'local': 'Rua Fagundes Varela, 690, Itaperi - Fortaleza/CE',
        'descricao': 'Prepare-se para a noite mais misteriosa do ano. O MaréVibes Halloween chega com uma proposta imersiva e exclusiva em Fortaleza, unindo cenografia temática de arrepiar e os melhores DJs da cena local.'
    }
    ambiente_teste = not MERCADOPAGO_TOKEN or MERCADOPAGO_TOKEN.startswith('TEST-')
    
    # Injeta a chave pública do reCAPTCHA v3 para os templates HTML
    recaptcha_site_key = os.getenv('RECAPTCHA_SITE_KEY', '')
    
    return dict(
        evento=evento_default, 
        ambiente_teste=ambiente_teste,
        recaptcha_site_key=recaptcha_site_key,
        mp_public_key=MP_PUBLIC_KEY, # Passa a chave pública para todos os templates
        vendas_encerradas=vendas_encerradas()
    )

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

    ingressos = db.relationship('Ingresso', backref='comprador', lazy=True, cascade='all, delete-orphan')


class Evento(db.Model):
    __tablename__ = 'eventos'
    
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False) # ex: 'marevibes-halloween-2026'
    titulo = db.Column(db.String(150), nullable=False)
    data_hora = db.Column(db.DateTime, nullable=False)
    local = db.Column(db.String(255), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    imagem_banner = db.Column(db.String(255), nullable=True)
    ativo = db.Column(db.Boolean, default=True)

    lotes = db.relationship('Lote', backref='evento', lazy=True, cascade='all, delete-orphan')


class Lote(db.Model):
    __tablename__ = 'lotes'

    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(db.Integer, db.ForeignKey('eventos.id'), nullable=True)
    nome = db.Column(db.String(50), nullable=False) # Ex: "1º Lote", "VIP"
    preco = db.Column(db.Float, nullable=False)
    quantidade_total = db.Column(db.Integer, nullable=False)
    ativo = db.Column(db.Boolean, default=True)

    ingressos = db.relationship('Ingresso', backref='lote_origem', lazy=True)


class Pedido(db.Model):
    __tablename__ = 'pedidos'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    pagamento_id = db.Column(db.String(100), unique=True, nullable=True) # ID retornado pelo Mercado Pago
    status = db.Column(db.String(20), default='pending') # pending, approved, cancelled
    total = db.Column(db.Float, nullable=False)
    metodo_pagamento = db.Column(db.String(20), nullable=True) # pix, credit_card
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    itens = db.relationship('ItemPedido', backref='pedido', lazy=True, cascade='all, delete-orphan')
    ingressos = db.relationship('Ingresso', backref='pedido_origem', lazy=True)


class ItemPedido(db.Model):
    __tablename__ = 'itens_pedido'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedidos.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    preco_unitario = db.Column(db.Float, nullable=False)
    
    lote = db.relationship('Lote')


class Ingresso(db.Model):
    __tablename__ = 'ingressos'

    id = db.Column(db.Integer, primary_key=True)
    codigo_qr = db.Column(db.String(50), unique=True, nullable=False)
    evento_nome = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='valido')
    data_compra = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    data_uso = db.Column(db.DateTime, nullable=True)
    
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=False)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedidos.id'), nullable=True)
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
            else:
                if not admin_user.is_admin or not admin_user.email_verificado:
                    admin_user.is_admin = True
                    admin_user.email_verificado = True

            # 1. Lote Promocional (R$ 1,00 - 10 unidades - Ativo)
            lote_promo = Lote.query.filter(Lote.nome.ilike('%promocional%')).first()
            if not lote_promo:
                lote_promo = Lote(nome='Lote Promocional (Teste)', preco=1.00, quantidade_total=10, ativo=True)
                db.session.add(lote_promo)
            else:
                lote_promo.nome = 'Lote Promocional (Teste)'
                lote_promo.preco = 1.00
                lote_promo.quantidade_total = 10

            # 2. 1º Lote (R$ 100,00 - 10 unidades)
            lote_1 = Lote.query.filter(Lote.nome.ilike('%1º lote%')).first()
            if not lote_1:
                lote_1 = Lote(nome='1º Lote', preco=100.00, quantidade_total=10, ativo=False)
                db.session.add(lote_1)
            else:
                lote_1.nome = '1º Lote'
                lote_1.preco = 100.00
                lote_1.quantidade_total = 10

            # 3. 2º Lote (R$ 200,00 - 10 unidades)
            lote_2 = Lote.query.filter(Lote.nome.ilike('%2º lote%')).first()
            if not lote_2:
                lote_2 = Lote(nome='2º Lote', preco=200.00, quantidade_total=10, ativo=False)
                db.session.add(lote_2)
            else:
                lote_2.nome = '2º Lote'
                lote_2.preco = 200.00
                lote_2.quantidade_total = 10

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[ERRO BANCO DE DADOS]: Falha ao inicializar banco: {str(e)}")

# --------------------------------------------------------------------------
# Decoradores e Funções Auxiliares
# --------------------------------------------------------------------------

def validar_recaptcha(token, action_esperada=None):
    secret_key = os.getenv('RECAPTCHA_SECRET_KEY', '')
    if not secret_key:
        return True

    if not token:
        return False

    url = 'https://www.google.com/recaptcha/api/siteverify'
    data = urllib.parse.urlencode({
        'secret': secret_key,
        'response': token
    }).encode('utf-8')

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            
            success = res_data.get('success', False)
            score = res_data.get('score', 0.0)
            action = res_data.get('action', '')

            if action_esperada and action != action_esperada:
                return False

            return success and score >= 0.5
    except Exception as e:
        print(f"[ERRO RECAPTCHA]: Falha na comunicação com a API do Google: {str(e)}")
        return False

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

def gerar_token_recuperacao(email):
    serializer = URLSafeTimedSerializer(app.secret_key)
    return serializer.dumps(email, salt='password-reset-salt')

def validar_token_recuperacao(token, max_age=3600):
    serializer = URLSafeTimedSerializer(app.secret_key)
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=max_age)
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
        return True
    except Exception as e:
        print(f"[ERRO CRÍTICO NO ENVIO DE E-MAIL]: {str(e)}")
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

def gerar_ingressos_para_pagamento(pagamento_id, usuario_id, lote_id, quantidade):
    lote = Lote.query.get(lote_id)
    if not lote:
        return False
        
    # Evita duplicação verificando se já existem ingressos gerados para o mesmo ID de pagamento
    if Ingresso.query.filter_by(pagamento_id=str(pagamento_id)).count() > 0:
        return False

    evento_titulo = lote.evento.titulo if (lote.evento and lote.evento.titulo) else "MaréVibes Halloween 2026"

    for _ in range(quantidade):
        novo_ingresso = Ingresso(
            codigo_qr=gerar_codigo_ingresso(),
            evento_nome=evento_titulo,
            usuario_id=usuario_id,
            lote_id=lote_id,
            pagamento_id=str(pagamento_id)
        )
        db.session.add(novo_ingresso)
    
    db.session.commit()
    return True

def gerar_ingressos_para_pedido(pedido_id, payment_id):
    pedido = Pedido.query.get(pedido_id)
    if not pedido:
        return False

    # Evita duplicação de ingressos para o mesmo pedido
    if Ingresso.query.filter_by(pedido_id=pedido.id).count() > 0:
        return False

    pedido.status = 'approved'
    pedido.pagamento_id = str(payment_id)

    for item in pedido.itens:
        evento_titulo = item.lote.evento.titulo if (item.lote and item.lote.evento and item.lote.evento.titulo) else "MaréVibes Halloween 2026"
        for _ in range(item.quantidade):
            novo_ingresso = Ingresso(
                codigo_qr=gerar_codigo_ingresso(),
                evento_nome=evento_titulo,
                usuario_id=pedido.usuario_id,
                lote_id=item.lote_id,
                pedido_id=pedido.id,
                pagamento_id=str(payment_id)
            )
            db.session.add(novo_ingresso)

    db.session.commit()
    return True

def validar_assinatura_mercadopago(req):
    x_signature = req.headers.get('x-signature')
    x_request_id = req.headers.get('x-request-id')
    
    if not x_signature or not MP_WEBHOOK_SECRET:
        return True
        
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

@app.route('/style.css')
def style_fallback():
    return send_from_directory('static/css', 'main.css')

# ==========================================================================
# 1. ROTAS PÚBLICAS (Visitantes e Autenticados)
# ==========================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/servicos')
def servicos():
    return render_template('servicos.html')

@app.route('/evento/marevibes-halloween')
def evento_marevibes():
    lotes = Lote.query.order_by(Lote.id.asc()).all()
    lote_ativo = Lote.query.filter_by(ativo=True).first()
    return render_template('eventos/marevibes_halloween.html', lote=lote_ativo, lotes=lotes)

@app.route('/termos-de-uso')
def termos_de_uso():
    return render_template('termos_de_uso.html')

@app.route('/politica-de-privacidade')
def politica_privacidade():
    return render_template('politica_privacidade.html')

@app.route('/compromisso')
def compromisso():
    return render_template('compromisso.html')

@app.route('/quem-somos')
def quem_somos():
    return render_template('quem_somos.html')

@app.route('/meia-entrada')
def meia_entrada():
    return render_template('meia_entrada.html')

@app.route('/faq')
def faq():
    return render_template('faq.html')

@app.route('/forcar-ativacao/<email>')
def forcar_ativacao(email):
    usuario = Usuario.query.filter_by(email=email.strip().lower()).first()
    if usuario:
        usuario.email_verificado = True
        db.session.commit()
        return f"Sucesso! A conta {email} foi ativada manualmente."
    return "Usuário não encontrado.", 404

@app.route('/contato', methods=['GET', 'POST'])
def contato():
    if request.method == 'POST':
        recaptcha_token = request.form.get('g-recaptcha-response')
        if not validar_recaptcha(recaptcha_token, action_esperada='contato'):
            flash('Falha na verificação anti-spam (reCAPTCHA). Tente novamente.', 'danger')
            return redirect(url_for('contato'))

        nome = request.form.get('nome', '').strip()
        email_cliente = request.form.get('email', '').strip()
        assunto = request.form.get('assunto', '').strip()
        mensagem = request.form.get('mensagem', '').strip()

        email_empresa = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME', ''))

        if not email_empresa:
            flash('Serviço de e-mail indisponível no momento.', 'warning')
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

        flash('Mensagem enviada com sucesso!', 'success')
        return redirect(url_for('contato'))

    return render_template('contato.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        recaptcha_token = request.form.get('g-recaptcha-response')
        if not validar_recaptcha(recaptcha_token, action_esperada='cadastro'):
            flash('Falha na validação de segurança (reCAPTCHA). Tente novamente.', 'danger')
            return redirect(url_for('cadastro'))

        try:
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip().lower()
            cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
            telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
            senha = request.form.get('senha', '')
            confirmar = request.form.get('confirmar_senha', '')

            if not nome or not email or not senha or not cpf:
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
                    flash('Este e-mail já está cadastrado. Faça login para continuar.', 'info')
                    return redirect(url_for('login'))

            if Usuario.query.filter_by(cpf=cpf).first():
                flash('O CPF informado já está associado a outra conta.', 'danger')
                return redirect(url_for('cadastro'))

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

            flash('Cadastro realizado com sucesso! Verifique seu e-mail para ativar a conta.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            flash('Erro interno ao realizar o cadastro. Tente novamente.', 'danger')
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

@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão encerrada com sucesso.', 'info')
    return redirect(url_for('login'))

@app.route('/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha():
    if request.method == 'POST':
        recaptcha_token = request.form.get('g-recaptcha-response')
        if not validar_recaptcha(recaptcha_token, action_esperada='esqueci_senha'):
            flash('Falha na validação de segurança (reCAPTCHA). Tente novamente.', 'danger')
            return redirect(url_for('esqueci_senha'))

        email = request.form.get('email', '').strip().lower()
        usuario = Usuario.query.filter_by(email=email).first()

        if usuario:
            token = gerar_token_recuperacao(usuario.email)
            link_redefinicao = url_for('redefinir_senha', token=token, _external=True)

            assunto = "[Dissonante Experiências] Instruções para redefinir sua senha"
            corpo = f"""Olá, {usuario.nome}!

Recebemos uma solicitação para redefinir a senha da sua conta.

Para criar uma nova senha, clique no link abaixo:
{link_redefinicao}

Atenção: Este link é válido por apenas 1 hora.

Atenciosamente,
Equipe Dissonante Experiências
"""
            threading.Thread(target=enviar_email_direto, args=(usuario.email, assunto, corpo)).start()

        flash(f'Enviamos as instruções de redefinição para o e-mail {email}, caso ele esteja cadastrado.', 'info')
        return redirect(url_for('esqueci_senha', email=email, enviado='1'))

    email_preenchido = request.args.get('email', '').strip()
    enviado = request.args.get('enviado') == '1'

    return render_template('esqueci_senha.html', email=email_preenchido, enviado=enviado)

@app.route('/redefinir-senha', defaults={'token': None}, methods=['GET', 'POST'])
@app.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
def redefinir_senha(token):
    usuario = None

    if token is None:
        if not session.get('usuario_id') or not session.get('autorizado_redefinir_senha'):
            flash('Acesso não autorizado.', 'danger')
            return redirect(url_for('configuracoes'))
            
        usuario = Usuario.query.get(session['usuario_id'])
    else:
        email = validar_token_recuperacao(token)
        if not email:
            flash('O link de redefinição é inválido ou expirou.', 'danger')
            return redirect(url_for('esqueci_senha'))

        usuario = Usuario.query.filter_by(email=email).first()

    if not usuario:
        flash('Usuário não encontrado.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        senha = request.form.get('senha', '')
        confirmar = request.form.get('confirmar_senha', '')

        if not senha or len(senha) < 6:
            flash('A senha deve ter no mínimo 6 caracteres.', 'warning')
            return render_template('redefinir_senha.html', token=token)

        if senha != confirmar:
            flash('As senhas digitadas não coincidem.', 'danger')
            return render_template('redefinir_senha.html', token=token)

        usuario.senha_hash = generate_password_hash(senha)
        db.session.commit()

        session.pop('autorizado_redefinir_senha', None)

        flash('Sua senha foi redefinida com sucesso!', 'success')
        
        if session.get('usuario_id'):
            return redirect(url_for('perfil'))
        return redirect(url_for('login'))

    return render_template('redefinir_senha.html', token=token)

@app.route('/carrinho')
def ver_carrinho():
    carrinho_dict = session.get('carrinho', {})
    itens_carrinho = list(carrinho_dict.values())
    total = sum(item['preco'] * item['quantidade'] for item in itens_carrinho)
    return render_template('carrinho.html', carrinho=itens_carrinho, total=total)

@app.route('/carrinho/adicionar-multiplo', methods=['POST'])
def adicionar_carrinho_multiplo():
    # Coleta as quantidades do formulário
    qty_promo = int(request.form.get('qty_promocional', 0))
    qty_lote1 = int(request.form.get('qty_lote1', 0))
    qty_lote2 = int(request.form.get('qty_lote2', 0))

    if (qty_promo + qty_lote1 + qty_lote2) <= 0:
        flash('Selecione ao menos um ingresso para continuar.', 'warning')
        return redirect(url_for('evento_marevibes'))

    lotes_db = Lote.query.all()
    
    # Mapeamento dos lotes no banco
    mapa_lotes = {
        'promo': next((l for l in lotes_db if 'promocional' in l.nome.lower()), None),
        'lote1': next((l for l in lotes_db if '1º lote' in l.nome.lower()), None),
        'lote2': next((l for l in lotes_db if '2º lote' in l.nome.lower()), None)
    }

    quantidades = [('promo', qty_promo), ('lote1', qty_lote1), ('lote2', qty_lote2)]
    carrinho = session.get('carrinho', {})

    for chave, qtd in quantidades:
        if qtd > 0:
            lote = mapa_lotes.get(chave)
            if not lote:
                continue

            # Validação de estoque disponível no banco
            vendidos = Ingresso.query.filter_by(lote_id=lote.id).count()
            disponiveis = lote.quantidade_total - vendidos
            
            # Quantidade atual já presente no carrinho + a nova quantidade
            str_lote_id = str(lote.id)
            qtd_no_carrinho = carrinho.get(str_lote_id, {}).get('quantidade', 0)

            if (qtd_no_carrinho + qtd) > disponiveis:
                flash(f'Desculpe, restam apenas {disponiveis} ingressos disponíveis para o {lote.nome}.', 'danger')
                return redirect(url_for('evento_marevibes'))

            # Adiciona ou atualiza o item no carrinho
            if str_lote_id in carrinho:
                carrinho[str_lote_id]['quantidade'] += qtd
            else:
                carrinho[str_lote_id] = {
                    'lote_id': lote.id,
                    'evento_nome': lote.evento.titulo if (hasattr(lote, 'evento') and lote.evento) else "MaréVibes Halloween 2026",
                    'lote_nome': lote.nome,
                    'preco': lote.preco,
                    'quantidade': qtd
                }

    session['carrinho'] = carrinho
    session.modified = True
    flash('Ingressos adicionados ao carrinho!', 'success')
    return redirect(url_for('ver_carrinho'))

@app.route('/carrinho/remover/<int:lote_id>', methods=['POST'])
def remover_carrinho(lote_id):
    carrinho = session.get('carrinho', {})
    str_lote_id = str(lote_id)
    if str_lote_id in carrinho:
        del carrinho[str_lote_id]
        session['carrinho'] = carrinho
        session.modified = True
        flash('Item removido do carrinho.', 'info')
    return redirect(url_for('ver_carrinho'))

@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    if not sdk:
        return jsonify({"status": "sdk_not_configured"}), 200

    if not validar_assinatura_mercadopago(request):
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
                
                # Trata pagamentos via ID do Pedido
                if ext_ref and ext_ref.startswith("PEDIDO_"):
                    pedido_id = int(ext_ref.split("_")[1])
                    pedido = Pedido.query.get(pedido_id)

                    if pedido:
                        ingressos_novos_criados = gerar_ingressos_para_pedido(pedido.id, payment_id)

                        if ingressos_novos_criados:
                            comprador = Usuario.query.get(pedido.usuario_id)
                            ingressos = Ingresso.query.filter_by(pedido_id=pedido.id).all()
                            
                            codigos_str = "\n".join([f"- [{ing.evento_nome}] Código: {ing.codigo_qr}" for ing in ingressos])

                            assunto_cliente = "[Dissonante Experiências] Seus ingressos estão prontos!"
                            corpo_cliente = f"""Olá, {comprador.nome}!

Seu pagamento referente ao Pedido #{pedido.id} foi confirmado com sucesso! 🎉

Seus Ingressos:
{codigos_str}

Atenciosamente,
Equipe Dissonante Experiências
"""
                            threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cliente, corpo_cliente)).start()

                # Trata pagamentos diretos via external_reference (usuario_id|lote_id|quantidade)
                else:
                    usr_id, lote_id, qtd = extrair_ref_externa(ext_ref)
                    if usr_id and lote_id and qtd:
                        ingressos_novos_criados = gerar_ingressos_para_pagamento(payment_id, usr_id, lote_id, qtd)

                        if ingressos_novos_criados:
                            comprador = Usuario.query.get(usr_id)
                            ingressos = Ingresso.query.filter_by(pagamento_id=str(payment_id)).all()

                            if comprador and ingressos:
                                codigos_str = "\n".join([f"- [{ing.evento_nome}] Código: {ing.codigo_qr}" for ing in ingressos])

                                assunto_cliente = "[Dissonante Experiências] Seus ingressos estão prontos!"
                                corpo_cliente = f"""Olá, {comprador.nome}!

Seu pagamento referente ao Pagamento #{payment_id} foi confirmado com sucesso! 🎉

Seus Ingressos:
{codigos_str}

Atenciosamente,
Equipe Dissonante Experiências
"""
                                threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cliente, corpo_cliente)).start()

            elif status in ["refunded", "charged_back", "cancelled"]:
                ext_ref = payment_info.get("external_reference", "")
                pedido = None

                if ext_ref and ext_ref.startswith("PEDIDO_"):
                    pedido_id = int(ext_ref.split("_")[1])
                    pedido = Pedido.query.get(pedido_id)

                ingressos_para_remover = Ingresso.query.filter_by(pagamento_id=str(payment_id)).all()
                comprador = Usuario.query.get(pedido.usuario_id) if pedido else None

                if not comprador and ingressos_para_remover:
                    comprador = Usuario.query.get(ingressos_para_remover[0].usuario_id)

                for ing in ingressos_para_remover:
                    db.session.delete(ing)

                if pedido:
                    pedido.status = status
                    
                db.session.commit()

                if comprador:
                    assunto_cancelamento = "[Dissonante Experiências] Cancelamento de Ingresso / Estorno"
                    corpo_cancelamento = f"""Olá, {comprador.nome}.

Identificamos a devolução/estorno do pagamento (ID: {payment_id}). Os ingressos deste pedido foram cancelados.

Atenciosamente,
Equipe Dissonante Experiências
"""
                    threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cancelamento, corpo_cancelamento)).start()

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO WEBHOOK]: {str(e)}")

    return jsonify({"status": "ok"}), 200

# ==========================================================================
# 2. ROTAS AUTENTICADAS DO CLIENTE (@cliente_required)
# ==========================================================================

LIMITE_MAXIMO_LOTE = 5

@app.route('/meus-ingressos')
@cliente_required
def meus_ingressos():
    usuario_id = session.get('usuario_id')
    ingressos = Ingresso.query.filter_by(usuario_id=usuario_id).order_by(Ingresso.data_compra.desc()).all()
    return render_template('meus_ingressos.html', ingressos=ingressos)

@app.route('/perfil')
@cliente_required
def perfil():
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        session.clear()
        flash('Sessão inválida. Por favor, faça login novamente.', 'warning')
        return redirect(url_for('login'))

    return render_template('perfil.html', usuario=usuario)

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

@app.route('/configuracoes', methods=['GET', 'POST'])
@cliente_required
def configuracoes():
    usuario = Usuario.query.get(session['usuario_id'])
    
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
        
        if not nome or not email:
            flash('Nome e E-mail são campos obrigatórios.', 'warning')
            return redirect(url_for('configuracoes'))

        if email != usuario.email:
            if Usuario.query.filter_by(email=email).first():
                flash('Este e-mail já está em uso por outro usuário.', 'danger')
                return redirect(url_for('configuracoes'))
            usuario.email = email
            usuario.email_verificado = False
            session['usuario_email'] = email

        usuario.nome = nome
        usuario.telefone = telefone
        
        db.session.commit()
        session['usuario_nome'] = usuario.nome
        
        flash('Configurações salvas com sucesso!', 'success')
        return redirect(url_for('configuracoes'))

    return render_template('configuracoes.html', usuario=usuario)

@app.route('/configuracoes/verificar-senha', methods=['POST'])
@cliente_required
def verificar_senha_para_redefinir():
    usuario = Usuario.query.get(session['usuario_id'])
    senha_atual = request.form.get('senha_atual', '')

    if check_password_hash(usuario.senha_hash, senha_atual):
        session['autorizado_redefinir_senha'] = True
        return redirect(url_for('redefinir_senha'))
    
    flash('Senha atual incorreta.', 'danger')
    return redirect(url_for('configuracoes'))

@app.route('/deletar-conta', methods=['POST'])
@cliente_required
def deletar_conta():
    usuario = Usuario.query.get(session['usuario_id'])
    senha_confirmacao = request.form.get('confirm_senha', '')

    if not check_password_hash(usuario.senha_hash, senha_confirmacao):
        flash('Senha incorreta. A conta não foi excluída.', 'danger')
        return redirect(url_for('configuracoes'))

    try:
        db.session.delete(usuario)
        db.session.commit()
        session.clear()
        flash('Sua conta foi excluída permanentemente.', 'info')
        return redirect(url_for('login'))
    except Exception as e:
        db.session.rollback()
        flash('Não foi possível excluir sua conta no momento.', 'danger')
        return redirect(url_for('configuracoes'))

@app.route('/favoritar', methods=['POST'])
@cliente_required
def favoritar():
    data = request.get_json(silent=True) or {}
    evento_id = data.get('evento_id')

    if not evento_id:
        return jsonify({'status': 'error', 'message': 'ID do evento ausente'}), 400

    eventos_map = {
        'marevibes': {
            'id': 'marevibes',
            'nome': 'MaréVibes Halloween 2026',
            'data': '31 OUT 2026'
        }
    }

    favoritos = session.get('favoritos', [])
    item_existente = next((item for item in favoritos if isinstance(item, dict) and item.get('id') == evento_id), None)

    if item_existente:
        favoritos.remove(item_existente)
        favoritado = False
    else:
        evento_info = eventos_map.get(evento_id, {'id': evento_id, 'nome': evento_id, 'data': 'Em breve'})
        favoritos.append(evento_info)
        favoritado = True

    session['favoritos'] = favoritos
    session.modified = True

    return jsonify({
        'status': 'success',
        'favoritado': favoritado,
        'total_favoritos': len(favoritos)
    })

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    # EXIGÊNCIA DE LOGIN APENAS NA HORA DE FINALIZAR A COMPRA
    if 'usuario_id' not in session:
        flash('Para finalizar a sua compra, faça login ou crie uma conta.', 'info')
        return redirect(url_for('login'))

    if vendas_encerradas():
        flash('As vendas de ingressos para este evento foram encerradas.', 'danger')
        return redirect(url_for('evento_marevibes'))

    usuario_id = session.get('usuario_id')
    usuario_atual = Usuario.query.get(usuario_id)

    if not usuario_atual:
        flash('Sua sessão expirou. Faça login novamente.', 'warning')
        return redirect(url_for('login'))

    # Coleta dos itens DIRETAMENTE do carrinho na sessão
    carrinho = session.get('carrinho', {})
    if not carrinho:
        flash('Seu carrinho está vazio. Selecione seus ingressos para continuar.', 'warning')
        return redirect(url_for('evento_marevibes'))

    # VALIDAÇÃO DO LIMITADOR DE INGRESSOS POR CLIENTE
    qtd_no_carrinho = sum(item.get('quantidade', 0) for item in carrinho.values())
    ingressos_ja_comprados = Ingresso.query.filter_by(usuario_id=usuario_id).count()

    if (ingressos_ja_comprados + qtd_no_carrinho) > LIMITE_MAXIMO_LOTE:
        disponiveis_para_usuario = max(0, LIMITE_MAXIMO_LOTE - ingressos_ja_comprados)
        if disponiveis_para_usuario > 0:
            flash(
                f'Você já possui {ingressos_ja_comprados} ingresso(s). '
                f'O limite máximo por cliente é de {LIMITE_MAXIMO_LOTE}. '
                f'Você só pode adicionar mais {disponiveis_para_usuario} ingresso(s).', 
                'danger'
            )
        else:
            flash(
                f'Você já atingiu o limite máximo de {LIMITE_MAXIMO_LOTE} ingressos por cliente para este evento.', 
                'danger'
            )
        return redirect(url_for('ver_carrinho'))

    ordem_compra = []
    total_pedido = 0.0

    for item_key, item_data in carrinho.items():
        lote_obj = Lote.query.get(item_data.get('lote_id'))
        if lote_obj:
            quantidade = item_data.get('quantidade', 0)
            if quantidade > 0:
                subtotal = quantidade * lote_obj.preco
                total_pedido += subtotal
                ordem_compra.append({
                    'lote': lote_obj,
                    'nome': lote_obj.nome,
                    'preco_unitario': lote_obj.preco,
                    'quantidade': quantidade,
                    'subtotal': subtotal
                })

    if not ordem_compra or total_pedido <= 0:
        flash('Nenhum ingresso válido foi encontrado no seu carrinho.', 'warning')
        return redirect(url_for('evento_marevibes'))

    # Processamento do Pagamento quando o formulário dentro do checkout é submetido
    if request.method == 'POST':
        metodo_pagamento = request.form.get('metodo_pagamento', 'pix')

        # Validação de estoque para todos os itens do carrinho
        for item in ordem_compra:
            lote_obj = item['lote']
            vendidos = Ingresso.query.filter_by(lote_id=lote_obj.id).count()
            disponiveis = lote_obj.quantidade_total - vendidos
            if item['quantidade'] > disponiveis:
                flash(f'Restam apenas {disponiveis} ingresso(s) no {lote_obj.nome}.', 'danger')
                return redirect(url_for('evento_marevibes'))

        # Montagem dos dados do pagador
        nome_completo = (usuario_atual.nome or 'Cliente').strip().split(' ', 1)
        first_name = nome_completo[0]
        last_name = nome_completo[1] if len(nome_completo) > 1 else "Silva"
        cpf_usuario = re.sub(r'\D', '', usuario_atual.cpf or '')
        ddd_tel, num_tel = extrair_ddd_e_numero(usuario_atual.telefone)

        payer_payload = {
            "email": usuario_atual.email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": {"area_code": ddd_tel, "number": num_tel},
            "identification": {"type": "CPF", "number": cpf_usuario}
        }

        if not sdk:
            flash('Ambiente de demonstração: Configure MP_ACCESS_TOKEN no .env.', 'info')
            return redirect(url_for('evento_marevibes'))

        qtd_total_ingressos = sum(item['quantidade'] for item in ordem_compra)

        try:
            # 1. CRIAR O REGISTRO DO PEDIDO NO BANCO
            novo_pedido = Pedido(
                usuario_id=usuario_atual.id,
                total=float(total_pedido),
                status='pending',
                metodo_pagamento=metodo_pagamento
            )
            db.session.add(novo_pedido)
            db.session.flush() # Gera o ID do pedido antes de fechar a transação

            # 2. SALVAR OS ITENS ASSOCIADOS AO PEDIDO
            for item in ordem_compra:
                item_pedido = ItemPedido(
                    pedido_id=novo_pedido.id,
                    lote_id=item['lote'].id,
                    quantidade=item['quantidade'],
                    preco_unitario=item['preco_unitario']
                )
                db.session.add(item_pedido)

            db.session.commit()

        except Exception as e:
            db.session.rollback()
            flash('Erro interno ao registrar o pedido. Tente novamente.', 'danger')
            return redirect(url_for('evento_marevibes'))

        # Trata Pagamento via PIX
        if metodo_pagamento == 'pix':
            data_expiracao = datetime.now(timezone.utc) + timedelta(minutes=15)
            payment_data = {
                "transaction_amount": float(total_pedido),
                "description": f"Pedido #{novo_pedido.id} - MaréVibes Halloween",
                "payment_method_id": "pix",
                "date_of_expiration": data_expiracao.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                "external_reference": f"PEDIDO_{novo_pedido.id}",
                "payer": payer_payload
            }

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status_code = payment_response.get("status")

                if status_code in [200, 201] and payment.get("status") in ["pending", "approved"]:
                    payment_id_str = str(payment.get("id"))
                    novo_pedido.pagamento_id = payment_id_str
                    db.session.commit()

                    pix_info = payment.get("point_of_interaction", {}).get("transaction_data", {})
                    session['compra_atual'] = {
                        'metodo_pagamento': 'pix',
                        'payment_id': payment_id_str,
                        'pedido_id': novo_pedido.id,
                        'total': float(total_pedido),
                        'quantidade': qtd_total_ingressos,
                        'qr_code': pix_info.get("qr_code"),
                        'qr_code_base64': pix_info.get("qr_code_base64")
                    }
                    # Limpa o carrinho após gerar o Pix
                    session.pop('carrinho', None)
                    return redirect(url_for('pagamento'))
                else:
                    db.session.delete(novo_pedido)
                    db.session.commit()
                    cause = payment.get("cause", [])
                    detalhe = cause[0].get("description") if cause else payment.get("message", "Erro desconhecido")
                    flash(f'Erro no processamento do Pix: {detalhe}', 'danger')
                    return redirect(url_for('evento_marevibes'))

            except Exception as e:
                db.session.delete(novo_pedido)
                db.session.commit()
                flash('Falha de conexão com o Mercado Pago. Tente novamente.', 'danger')
                return redirect(url_for('evento_marevibes'))

        # Trata Pagamento via Cartão de Crédito
        elif metodo_pagamento == 'credit_card':
            token = request.form.get('token')
            installments = int(request.form.get('installments', 1))
            payment_method_id = request.form.get('payment_method_id')
            issuer_id = request.form.get('issuer_id')

            if installments > 2:
                installments = 2

            if not token or not payment_method_id:
                db.session.delete(novo_pedido)
                db.session.commit()
                flash('Dados do cartão incompletos.', 'warning')
                return redirect(url_for('evento_marevibes'))

            payment_data = {
                "transaction_amount": float(total_pedido),
                "token": token,
                "description": f"Pedido #{novo_pedido.id} - MaréVibes Halloween",
                "installments": installments,
                "payment_method_id": payment_method_id,
                "external_reference": f"PEDIDO_{novo_pedido.id}",
                "payer": payer_payload
            }

            if issuer_id and str(issuer_id).strip() not in ['', 'null', 'undefined']:
                payment_data["issuer_id"] = str(issuer_id).strip()

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status = payment.get("status")
                payment_id = payment.get("id")

                if status == "approved":
                    novo_pedido.pagamento_id = str(payment_id)
                    novo_pedido.status = 'approved'
                    
                    # Emite os ingressos com base na estrutura de itens do Pedido
                    gerar_ingressos_para_pedido(novo_pedido.id, payment_id)

                    ingressos = Ingresso.query.filter_by(pedido_id=novo_pedido.id).all()
                    codigos_str = "\n".join([f"- [{ing.evento_nome}] Código: {ing.codigo_qr}" for ing in ingressos])

                    assunto_cliente = "[Dissonante Experiências] Seus ingressos estão prontos!"
                    corpo_cliente = f"""Olá, {usuario_atual.nome}!

Seu pagamento via Cartão de Crédito referente ao Pedido #{novo_pedido.id} foi confirmado! 🎉

Detalhes do Pedido:
--------------------------------------------------
Evento: MaréVibes Halloween 2026
Quantidade Total: {qtd_total_ingressos}
ID Transação: {payment_id}

Seus Ingressos:
{codigos_str}

Atenciosamente,
Equipe Dissonante Experiências
"""
                    threading.Thread(target=enviar_email_direto, args=(usuario_atual.email, assunto_cliente, corpo_cliente)).start()

                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'approved',
                        'payment_id': str(payment_id),
                        'pedido_id': novo_pedido.id,
                        'total': float(total_pedido),
                        'quantidade': qtd_total_ingressos
                    }

                    # Limpa o carrinho após aprovação do Cartão
                    session.pop('carrinho', None)

                    flash('Pagamento aprovado com sucesso!', 'success')
                    return redirect(url_for('pagamento'))

                elif status == "in_process":
                    novo_pedido.pagamento_id = str(payment_id)
                    db.session.commit()

                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'in_process',
                        'payment_id': str(payment_id),
                        'pedido_id': novo_pedido.id,
                        'total': float(total_pedido),
                        'quantidade': qtd_total_ingressos
                    }
                    # Limpa o carrinho após envio para análise
                    session.pop('carrinho', None)
                    flash('Pagamento em análise pela operadora do cartão.', 'info')
                    return redirect(url_for('pagamento'))
                else:
                    db.session.delete(novo_pedido)
                    db.session.commit()
                    status_detail = payment.get("status_detail", "Cartão recusado.")
                    flash(f'Transação não autorizada: {status_detail}.', 'danger')
                    return redirect(url_for('evento_marevibes'))

            except Exception as e:
                db.session.delete(novo_pedido)
                db.session.commit()
                flash('Falha na comunicação com a operadora do cartão.', 'danger')
                return redirect(url_for('evento_marevibes'))

    # Método GET: Renderiza o resumo do pedido no checkout vindo do carrinho
    return render_template(
        'checkout.html',
        usuario=usuario_atual,
        ordem_compra=ordem_compra,
        total_pedido=total_pedido
    )

@app.route('/pagamento')
@cliente_required
def pagamento():
    compra = session.get('compra_atual')
    if not compra:
        flash('Nenhuma transação pendente encontrada.', 'warning')
        return redirect(url_for('index'))
        
    return render_template('status_pagamento.html', compra=compra)

@app.route('/api/checar-status-pagamento/<payment_id>')
@cliente_required
def checar_status_pagamento(payment_id):
    if not sdk:
        return jsonify({'status': 'error', 'message': 'Mercado Pago não configurado'}), 500

    try:
        payment_info = sdk.payment().get(payment_id).get("response", {})
        status = payment_info.get("status")

        if status == 'approved':
            compra = session.get('compra_atual', {})
            user_id = session.get('usuario_id')
            lote_id = compra.get('lote_id')
            quantidade = compra.get('quantidade', 1)

            if user_id and lote_id:
                criou_agora = gerar_ingressos_para_pagamento(payment_id, user_id, lote_id, quantidade)

                if criou_agora:
                    comprador = Usuario.query.get(user_id)
                    ingressos = Ingresso.query.filter_by(pagamento_id=str(payment_id)).all()
                    
                    codigos_str = "\n".join([f"- Código: {ing.codigo_qr}" for ing in ingressos])

                    assunto_cliente = "[Dissonante Experiências] Seus ingressos estão prontos!"
                    corpo_cliente = f"""Olá, {comprador.nome}!

Seu pagamento via PIX foi confirmado com sucesso! 🎉

Seus Ingressos:
{codigos_str}

Atenciosamente,
Equipe Dissonante Experiências
"""
                    threading.Thread(target=enviar_email_direto, args=(comprador.email, assunto_cliente, corpo_cliente)).start()

            if 'compra_atual' in session:
                session['compra_atual']['status'] = 'approved'

            return jsonify({'status': 'approved', 'redirect_url': url_for('meus_ingressos')})

        return jsonify({'status': status})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ==========================================================================
# 3. ROTAS ADMINISTRATIVAS (@admin_required)
# ==========================================================================

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

@app.route('/admin/validar', methods=['GET', 'POST'], endpoint='validar_ingresso')
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
