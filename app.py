import os
import re
import random
import string
import threading
import smtplib
import hmac
import hashlib
import json
import uuid
import urllib.request
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from functools import wraps
from dotenv import load_dotenv
import mercadopago
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, not_
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature
from flask_migrate import Migrate

load_dotenv()

app = Flask(__name__)

# --------------------------------------------------------------------------
# Constantes Globais
# --------------------------------------------------------------------------
TZ_BRASILIA = timezone(timedelta(hours=-3))
DATA_LIMITE_VENDAS = datetime(2026, 10, 31, 19, 0, 0, tzinfo=TZ_BRASILIA)
MINUTOS_RESERVA = 15
LIMITE_MAXIMO_LOTE = 5

def vendas_encerradas():
    return datetime.now(TZ_BRASILIA) >= DATA_LIMITE_VENDAS

# --------------------------------------------------------------------------
# Configurações do App e Banco de Dados
# --------------------------------------------------------------------------
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(24)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

database_url = os.environ.get('DATABASE_URL')
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if "sslmode" not in database_url and "localhost" not in database_url:
        database_url += "?sslmode=require" if "?" not in database_url else "&sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///dev.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db, render_as_batch=True)

# --------------------------------------------------------------------------
# Mercado Pago & Configurações Monetárias (Decimal)
# --------------------------------------------------------------------------
TAXAS_MP = {
    'pix': Decimal('0.0099'),
    'credit_card_1x': Decimal('0.0499'),
    'credit_card_2x': Decimal('0.1462')
}

MENSAGENS_ERRO_MP = {
    'cc_rejected_bad_filled_other': 'Dados do cartão incorretos. Verifique os dados digitados.',
    'cc_rejected_bad_filled_card_number': 'Número do cartão inválido.',
    'cc_rejected_bad_filled_date': 'Data de expiração do cartão inválida.',
    'cc_rejected_bad_filled_security_code': 'Código de segurança (CVV) inválido.',
    'cc_rejected_insufficient_amount': 'Saldo ou limite insuficiente no cartão.',
    'cc_rejected_card_disabled': 'O cartão está bloqueado ou desativado. Entre em contato com a emissora.',
    'cc_rejected_max_attempts_exceeded': 'Você excedeu o limite de tentativas permitidas. Tente mais tarde.',
    'cc_rejected_call_for_authorize': 'Você precisa autorizar o pagamento junto ao seu banco/emissora.',
    'cc_rejected_high_risk': 'Pagamento recusado por motivos de segurança.',
    'cc_rejected_by_issuer': 'Pagamento recusado pela operadora do cartão.',
    'cc_rejected_invalid_installments': 'O número de parcelas selecionado não é aceito por este cartão.',
    'invalid_installments': 'O número de parcelas selecionado não é válido para este tipo de cartão.'
}

def quantizar(valor):
    if not isinstance(valor, Decimal):
        valor = Decimal(str(valor))
    return valor.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

def calcular_valor_com_taxa_mp(valor_base, metodo_pagamento='pix', parcelas=1):
    if not isinstance(valor_base, Decimal):
        valor_base = Decimal(str(valor_base))

    if valor_base <= Decimal('0.00'):
        return {'valor_final': Decimal('0.00'), 'taxa': Decimal('0.00')}
    
    if metodo_pagamento == 'pix':
        taxa = TAXAS_MP['pix']
    elif metodo_pagamento == 'credit_card':
        taxa = TAXAS_MP['credit_card_2x'] if parcelas == 2 else TAXAS_MP['credit_card_1x']
    else:
        taxa = Decimal('0.00')

    valor_final = quantizar(valor_base / (Decimal('1.00') - taxa))
    taxa_val = quantizar(valor_final - valor_base)
    return {'valor_final': valor_final, 'taxa': taxa_val}

MERCADOPAGO_TOKEN = os.getenv('MP_ACCESS_TOKEN', '')
MP_PUBLIC_KEY = os.getenv('MP_PUBLIC_KEY', '')
MP_WEBHOOK_SECRET = os.getenv('MP_WEBHOOK_SECRET', '')
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN) if MERCADOPAGO_TOKEN else None

class MercadoPagoOrdersAPI:
    def __init__(self, access_token):
        self.access_token = access_token
        self.base_url = "https://api.mercadopago.com/v1/orders"

    def _headers(self, request_id=None):
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(uuid.uuid4())
        }
        if request_id:
            headers["X-Request-Id"] = request_id
        return headers

    def create_order(self, order_data, request_id=None):
        data = json.dumps(order_data).encode('utf-8')
        req = urllib.request.Request(self.base_url, data=data, headers=self._headers(request_id), method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode('utf-8')), response.status
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            try:
                return json.loads(error_body), e.code
            except Exception:
                return {"error": error_body}, e.code

    def get_order(self, order_id):
        url = f"{self.base_url}/{order_id}"
        req = urllib.request.Request(url, headers=self._headers(), method='GET')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode('utf-8')), response.status
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            try:
                return json.loads(error_body), e.code
            except Exception:
                return {"error": error_body}, e.code

orders_api = MercadoPagoOrdersAPI(MERCADOPAGO_TOKEN) if MERCADOPAGO_TOKEN else None

# --------------------------------------------------------------------------
# Injeção de Contexto Global (Templates)
# --------------------------------------------------------------------------
@app.context_processor
def inject_globals():
    evento_default = {
        'titulo': 'MaréVibes Halloween 2026',
        'data_hora': datetime(2026, 10, 31, 17, 0, tzinfo=TZ_BRASILIA),
        'local': 'Rua Fagundes Varela, 690, Itaperi - Fortaleza/CE',
        'descricao': 'Prepare-se para a noite mais misteriosa do ano.'
    }
    ambiente_teste = not MERCADOPAGO_TOKEN or MERCADOPAGO_TOKEN.startswith('TEST-')
    recaptcha_site_key = os.getenv('RECAPTCHA_SITE_KEY', '')
    
    return dict(
        evento=evento_default, 
        ambiente_teste=ambiente_teste,
        recaptcha_site_key=recaptcha_site_key,
        mp_public_key=MP_PUBLIC_KEY,
        vendas_encerradas=vendas_encerradas()
    )

# --------------------------------------------------------------------------
# Modelos do Banco de Dados
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
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    ingressos = db.relationship('Ingresso', backref='comprador', lazy=True, cascade='all, delete-orphan')

class Evento(db.Model):
    __tablename__ = 'eventos'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False)
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
    nome = db.Column(db.String(50), nullable=False)
    preco = db.Column(db.Numeric(10, 2), nullable=False)
    quantidade_total = db.Column(db.Integer, nullable=False)
    ativo = db.Column(db.Boolean, default=True)
    ingressos = db.relationship('Ingresso', backref='lote_origem', lazy=True)

class Pedido(db.Model):
    __tablename__ = 'pedidos'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    pagamento_id = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='pending')
    total = db.Column(db.Numeric(10, 2), nullable=False)
    metodo_pagamento = db.Column(db.String(20), nullable=True)
    data_criacao = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    itens = db.relationship('ItemPedido', backref='pedido', lazy=True, cascade='all, delete-orphan')
    ingressos = db.relationship('Ingresso', backref='pedido_origem', lazy=True)

class ItemPedido(db.Model):
    __tablename__ = 'itens_pedido'
    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedidos.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    preco_unitario = db.Column(db.Numeric(10, 2), nullable=False)
    lote = db.relationship('Lote')

class Ingresso(db.Model):
    __tablename__ = 'ingressos'
    id = db.Column(db.Integer, primary_key=True)
    codigo_qr = db.Column(db.String(50), unique=True, nullable=False)
    evento_nome = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='valido')
    data_compra = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    data_uso = db.Column(db.DateTime, nullable=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=False)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedidos.id'), nullable=True)
    pagamento_id = db.Column(db.String(100), nullable=True)

class ReservaCarrinho(db.Model):
    __tablename__ = 'reservas_carrinho'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(100), nullable=False, index=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data_expiracao = db.Column(db.DateTime, nullable=False, index=True)
    lote = db.relationship('Lote')

def inicializar_banco():
    try:
        email_admin = "administracao@dissonanteexperiencias.com"
        admin_user = Usuario.query.filter_by(email=email_admin).first()

        if not admin_user:
            senha_padrao = os.environ.get('ADMIN_DEFAULT_PASSWORD', 'DevOnlyAdmin123!')
            admin_user = Usuario(
                nome="Administrador",
                email=email_admin,
                cpf="00000000000",
                telefone="85999999999",
                senha_hash=generate_password_hash(senha_padrao),
                is_admin=True,
                email_verificado=True
            )
            db.session.add(admin_user)

        evento_db = Evento.query.filter_by(slug='marevibes-halloween-2026').first()
        if not evento_db:
            evento_db = Evento(
                slug='marevibes-halloween-2026',
                titulo='MaréVibes Halloween 2026',
                data_hora=datetime(2026, 10, 31, 20, 0, 0), # Salvo em UTC naive
                local='Rua Fagundes Varela, 690, Itaperi - Fortaleza/CE'
            )
            db.session.add(evento_db)
            db.session.flush()

        lotes_config = [
            {"nome": "teste", "preco": Decimal('1.00'), "quantidade_total": 10, "ativo": True},
            {"nome": "Lote Promocional", "preco": Decimal('162.00'), "quantidade_total": 10, "ativo": True},
            {"nome": "1º Lote - Meia", "preco": Decimal('178.20'), "quantidade_total": 16, "ativo": True},
            {"nome": "1º Lote - Inteira", "preco": Decimal('194.40'), "quantidade_total": 24, "ativo": True},
            {"nome": "2º Lote - Meia", "preco": Decimal('194.40'), "quantidade_total": 16, "ativo": False},
            {"nome": "2º Lote - Inteira", "preco": Decimal('226.80'), "quantidade_total": 24, "ativo": False},
            {"nome": "Cortesia", "preco": Decimal('0.00'), "quantidade_total": 10, "ativo": False},
        ]

        for cfg in lotes_config:
            lote_db = Lote.query.filter_by(evento_id=evento_db.id, nome=cfg["nome"]).first()
            if not lote_db:
                lote_db = Lote(
                    evento_id=evento_db.id,
                    nome=cfg["nome"],
                    preco=cfg["preco"],
                    quantidade_total=cfg["quantidade_total"],
                    ativo=cfg["ativo"]
                )
                db.session.add(lote_db)

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        print(f"[ERRO BANCO DE DADOS]: Falha ao inicializar dados padrão: {str(e)}")

# --------------------------------------------------------------------------
# Funções Auxiliares e Segurança
# --------------------------------------------------------------------------

def validar_recaptcha(token, action_esperada=None):
    secret_key = os.getenv('RECAPTCHA_SECRET_KEY', '')
    if not secret_key:
        return True
    if not token:
        return False

    url = 'https://www.google.com/recaptcha/api/siteverify'
    data = urllib.parse.urlencode({'secret': secret_key, 'response': token}).encode('utf-8')

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if action_esperada and res_data.get('action') != action_esperada:
                return False
            return res_data.get('success', False) and res_data.get('score', 0.0) >= 0.5
    except Exception as e:
        print(f"[ERRO RECAPTCHA]: {str(e)}")
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
            flash('Acesso restrito a administradores.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def gerar_token_confirmacao(email):
    return URLSafeTimedSerializer(app.secret_key).dumps(email, salt='email-confirm-salt')

def validar_token_confirmacao(token, max_age=86400):
    try:
        return URLSafeTimedSerializer(app.secret_key).loads(token, salt='email-confirm-salt', max_age=max_age)
    except (SignatureExpired, BadTimeSignature):
        return None

def gerar_token_recuperacao(email):
    return URLSafeTimedSerializer(app.secret_key).dumps(email, salt='password-reset-salt')

def validar_token_recuperacao(token, max_age=3600):
    try:
        return URLSafeTimedSerializer(app.secret_key).loads(token, salt='password-reset-salt', max_age=max_age)
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
        print(f"[ERRO DE ENVIO DE E-MAIL]: {str(e)}")
        return False

def enviar_email_confirmacao(usuario_email, usuario_nome, token):
    try:
        link = url_for('validar_email', token=token, _external=True)
        corpo = f"Olá {usuario_nome}!\n\nConfirme seu e-mail no link abaixo:\n{link}"
        threading.Thread(target=lambda: enviar_email_direto(usuario_email, "[Dissonante] Validação de E-mail", corpo)).start()
        return True
    except Exception:
        return False

def gerar_codigo_ingresso():
    while True:
        hash_aleatorio = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
        codigo = f"DISSONANTE-HLW-{hash_aleatorio}"
        if not Ingresso.query.filter_by(codigo_qr=codigo).first():
            return codigo

def gerar_ingressos_para_pedido(pedido_id, payment_id):
    pedido = Pedido.query.get(pedido_id)
    if not pedido or Ingresso.query.filter_by(pedido_id=pedido.id).count() > 0:
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
    if not MP_WEBHOOK_SECRET:
        return True

    x_signature = req.headers.get('x-signature')
    x_request_id = req.headers.get('x-request-id')
    
    if not x_signature or not x_request_id:
        return False
        
    parts = {}
    for item in x_signature.split(','):
        if '=' in item:
            key, val = item.strip().split('=', 1)
            parts[key] = val

    ts, v1 = parts.get('ts'), parts.get('v1')
    if not ts or not v1:
        return False

    body_json = req.get_json(silent=True) or {}
    data_id = (
        req.args.get('data.id') or 
        req.args.get('id') or 
        body_json.get('data', {}).get('id') or 
        body_json.get('id')
    )

    if not data_id:
        return False

    manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    hmac_obj = hmac.new(MP_WEBHOOK_SECRET.encode('utf-8'), manifest.encode('utf-8'), hashlib.sha256)
    return hmac_obj.hexdigest() == v1

def limpar_reservas_expiradas():
    agora_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    ReservaCarrinho.query.filter(ReservaCarrinho.data_expiracao < agora_naive).delete(synchronize_session=False)
    db.session.flush()

def obter_estoque_disponivel(lote_id, session_id_atual=None):
    limpar_reservas_expiradas()
    lote = Lote.query.get(lote_id)
    if not lote:
        return 0

    vendidos = Ingresso.query.filter_by(lote_id=lote_id).count()
    agora_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    
    query_reservas = db.session.query(func.sum(ReservaCarrinho.quantidade)).filter(
        ReservaCarrinho.lote_id == lote_id,
        ReservaCarrinho.data_expiracao > agora_naive
    )
    
    if session_id_atual:
        query_reservas = query_reservas.filter(ReservaCarrinho.session_id != session_id_atual)

    reservados = query_reservas.scalar() or 0
    return max(0, lote.quantidade_total - vendidos - reservados)

def converter_int_seguro(valor, padrao=0):
    try:
        val = int(valor)
        return max(0, val)
    except (ValueError, TypeError):
        return padrao

@app.route('/style.css')
def style_fallback():
    return send_from_directory('static/css', 'main.css')

# ==========================================================================
# ROTAS PÚBLICAS
# ==========================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/servicos')
def servicos():
    return render_template('servicos.html')

@app.route('/evento/marevibes-halloween')
def evento_marevibes():
    session_id = session.get('session_token')
    lotes = Lote.query.filter(not_(Lote.nome.ilike('%Cortesia%'))).order_by(Lote.id.asc()).all()
    lote_ativo = Lote.query.filter_by(ativo=True).first()

    mapa_chaves = {
        'teste': 'teste',
        'promocional': 'promo',
        '1º lote - meia': 'lote1_meia',
        '1º lote - inteira': 'lote1_inteira',
        '2º lote - meia': 'lote2_meia',
        '2º lote - inteira': 'lote2_inteira'
    }

    estoques = {}
    for lote in lotes:
        for termo, chave in mapa_chaves.items():
            if termo in lote.nome.lower():
                disponivel = obter_estoque_disponivel(lote.id, session_id_atual=session_id)
                estoques[chave] = min(5, disponivel) if lote.ativo else 0

    return render_template('eventos/marevibes_halloween.html', lote=lote_ativo, lotes=lotes, estoques=estoques)

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
        if not validar_recaptcha(request.form.get('g-recaptcha-response'), action_esperada='contato'):
            flash('Falha na verificação reCAPTCHA. Tente novamente.', 'danger')
            return redirect(url_for('contato'))

        nome = request.form.get('nome', '').strip()
        email_cliente = request.form.get('email', '').strip()
        assunto = request.form.get('assunto', '').strip()
        mensagem = request.form.get('mensagem', '').strip()
        email_empresa = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME', ''))

        if email_empresa:
            corpo = f"Nome: {nome}\nEmail: {email_cliente}\nAssunto: {assunto}\n\nMensagem:\n{mensagem}"
            threading.Thread(target=lambda: enviar_email_direto(email_empresa, f"[Contato] {assunto}", corpo, email_cliente)).start()
            flash('Mensagem enviada com sucesso!', 'success')

        return redirect(url_for('contato'))
    return render_template('contato.html')

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        if not validar_recaptcha(request.form.get('g-recaptcha-response'), action_esperada='cadastro'):
            flash('Falha na validação de segurança (reCAPTCHA).', 'danger')
            return redirect(url_for('cadastro'))

        try:
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip().lower()
            cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
            telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
            senha = request.form.get('senha', '')

            if not nome or not email or not senha or not cpf:
                flash('Preencha todos os campos obrigatórios.', 'warning')
                return redirect(url_for('cadastro'))

            if Usuario.query.filter_by(email=email).first():
                flash('Este e-mail já possui cadastro.', 'info')
                return redirect(url_for('login'))

            novo_usuario = Usuario(
                nome=nome,
                email=email,
                cpf=cpf,
                telefone=telefone,
                senha_hash=generate_password_hash(senha),
                email_verificado=False
            )
            db.session.add(novo_usuario)
            db.session.commit()

            token = gerar_token_confirmacao(novo_usuario.email)
            enviar_email_confirmacao(novo_usuario.email, novo_usuario.nome, token)

            flash('Cadastro realizado! Verifique seu e-mail.', 'success')
            return redirect(url_for('login'))

        except Exception:
            db.session.rollback()
            flash('Erro ao realizar o cadastro.', 'danger')

    return render_template('cadastro.html')

@app.route('/validar-email/<token>')
def validar_email(token):
    email = validar_token_confirmacao(token)
    if not email:
        return render_template('email_confirmado.html', sucesso=False, mensagem="O link expirou ou é inválido.")

    usuario = Usuario.query.filter_by(email=email).first()
    if usuario and not usuario.email_verificado:
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
                flash('Sua conta ainda não foi ativada. Reenviamos o link por e-mail.', 'warning')
                return redirect(url_for('login'))

            session.permanent = True
            session['usuario_id'] = usuario.id
            session['usuario_nome'] = usuario.nome
            session['usuario_email'] = usuario.email
            session['is_admin'] = usuario.is_admin

            return redirect(url_for('admin_dashboard') if usuario.is_admin else url_for('perfil'))
        
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
        if not validar_recaptcha(request.form.get('g-recaptcha-response'), action_esperada='esqueci_senha'):
            flash('Falha reCAPTCHA. Tente novamente.', 'danger')
            return redirect(url_for('esqueci_senha'))

        email = request.form.get('email', '').strip().lower()
        usuario = Usuario.query.filter_by(email=email).first()

        if usuario:
            token = gerar_token_recuperacao(usuario.email)
            link = url_for('redefinir_senha', token=token, _external=True)
            corpo = f"Olá {usuario.nome}!\n\nRedefina sua senha acessando:\n{link}"
            threading.Thread(target=lambda: enviar_email_direto(usuario.email, "[Dissonante] Instruções de Senha", corpo)).start()

        flash(f'Enviamos as instruções para o e-mail informado.', 'info')
        return redirect(url_for('esqueci_senha', email=email, enviado='1'))

    return render_template('esqueci_senha.html', email=request.args.get('email', ''), enviado=request.args.get('enviado') == '1')

@app.route('/redefinir-senha/<token>', methods=['GET', 'POST'])
def redefinir_senha(token):
    email = validar_token_recuperacao(token)
    if not email:
        flash('O link de redefinição expirou.', 'danger')
        return redirect(url_for('esqueci_senha'))

    usuario = Usuario.query.filter_by(email=email).first()
    if request.method == 'POST':
        senha = request.form.get('senha', '')
        confirmar = request.form.get('confirmar_senha', '')

        if not senha or senha != confirmar or len(senha) < 6:
            flash('As senhas devem coincidir e ter ao menos 6 caracteres.', 'warning')
            return render_template('redefinir_senha.html', token=token)

        usuario.senha_hash = generate_password_hash(senha)
        db.session.commit()
        flash('Senha redefinida com sucesso!', 'success')
        return redirect(url_for('login'))

    return render_template('redefinir_senha.html', token=token)

@app.route('/carrinho')
def ver_carrinho():
    session_id = session.get('session_token')
    carrinho_dict = session.get('carrinho', {})
    tempo_restante_segundos = 0
    
    agora_utc = datetime.now(timezone.utc)
    agora_naive = agora_utc.replace(tzinfo=None)

    if session_id and carrinho_dict:
        primeira_reserva = ReservaCarrinho.query.filter(
            ReservaCarrinho.session_id == session_id,
            ReservaCarrinho.data_expiracao > agora_naive
        ).order_by(ReservaCarrinho.data_expiracao.asc()).first()

        if primeira_reserva:
            expiracao = primeira_reserva.data_expiracao
            delta = (expiracao - agora_naive).total_seconds()
            tempo_restante_segundos = max(0, int(delta))
        else:
            session.pop('carrinho', None)
            carrinho_dict = {}

    subtotal = sum(
        (Decimal(str(item['preco'])) * item['quantidade'] for item in carrinho_dict.values()), 
        Decimal('0.00')
    )

    calc_pix = calcular_valor_com_taxa_mp(subtotal, metodo_pagamento='pix')
    calc_cartao_1x = calcular_valor_com_taxa_mp(subtotal, metodo_pagamento='credit_card', parcelas=1)
    calc_cartao_2x = calcular_valor_com_taxa_mp(subtotal, metodo_pagamento='credit_card', parcelas=2)

    resumo_financeiro = {
        'subtotal': subtotal,
        'taxa_pix': calc_pix['taxa'],
        'total_pix': calc_pix['valor_final'],
        'total_cartao_1x': calc_cartao_1x['valor_final'],
        'total_cartao_2x': calc_cartao_2x['valor_final'],
        'parcela_2x': quantizar(calc_cartao_2x['valor_final'] / Decimal('2'))
    }

    return render_template(
        'carrinho.html', 
        carrinho=list(carrinho_dict.values()), 
        resumo=resumo_financeiro,
        tempo_restante_segundos=tempo_restante_segundos
    )

@app.route('/carrinho/adicionar-multiplo', methods=['POST'])
def adicionar_carrinho_multiplo():
    if 'session_token' not in session:
        session['session_token'] = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    
    if vendas_encerradas():
        flash('As vendas para este evento já foram encerradas.', 'danger')
        return redirect(url_for('evento_marevibes'))

    session_id = session['session_token']
    carrinho = session.get('carrinho', {})
    lotes_db = Lote.query.all()

    quantidades = [
        ('teste', converter_int_seguro(request.form.get('qty_teste'))),
        ('promocional', converter_int_seguro(request.form.get('qty_promo'))),
        ('1º lote - meia', converter_int_seguro(request.form.get('qty_lote1_meia'))),
        ('1º lote - inteira', converter_int_seguro(request.form.get('qty_lote1_inteira'))),
        ('2º lote - meia', converter_int_seguro(request.form.get('qty_lote2_meia'))),
        ('2º lote - inteira', converter_int_seguro(request.form.get('qty_lote2_inteira')))
    ]

    try:
        itens_adicionados = 0
        agora = datetime.now(timezone.utc).replace(tzinfo=None)
        expiracao = agora + timedelta(minutes=MINUTOS_RESERVA)

        for termo, qtd in quantidades:
            if qtd > 0:
                lote_map = next((l for l in lotes_db if termo in l.nome.lower()), None)
                if not lote_map or not lote_map.ativo:
                    continue

                lote = db.session.query(Lote).filter_by(id=lote_map.id).with_for_update().first()
                disponiveis = obter_estoque_disponivel(lote.id, session_id_atual=session_id)
                str_lote_id = str(lote.id)

                qtd_no_carrinho = carrinho.get(str_lote_id, {}).get('quantidade', 0)
                nova_qtd_total = qtd_no_carrinho + qtd

                if qtd > disponiveis:
                    db.session.rollback()
                    flash(f'Restam apenas {disponiveis} ingressos disponíveis no lote {lote.nome}.', 'danger')
                    return redirect(url_for('evento_marevibes'))

                if nova_qtd_total > LIMITE_MAXIMO_LOTE:
                    db.session.rollback()
                    flash(f'Você só pode ter no máximo {LIMITE_MAXIMO_LOTE} ingressos do lote {lote.nome}.', 'warning')
                    return redirect(url_for('evento_marevibes'))

                reserva = ReservaCarrinho.query.filter_by(session_id=session_id, lote_id=lote.id).first()
                if reserva:
                    reserva.quantidade = nova_qtd_total
                    reserva.data_expiracao = expiracao
                else:
                    db.session.add(ReservaCarrinho(
                        session_id=session_id, 
                        lote_id=lote.id, 
                        quantidade=nova_qtd_total, 
                        data_expiracao=expiracao
                    ))

                carrinho[str_lote_id] = {
                    'lote_id': lote.id,
                    'evento_nome': lote.evento.titulo if getattr(lote, 'evento', None) else "Evento",
                    'lote_nome': lote.nome,
                    'preco': float(lote.preco),
                    'quantidade': nova_qtd_total
                }
                itens_adicionados += 1

        if itens_adicionados == 0:
            db.session.rollback()
            flash('Selecione ao menos um lote válido.', 'warning')
            return redirect(url_for('evento_marevibes'))

        db.session.commit()
        session['carrinho'] = carrinho
        session.modified = True
        flash('Ingressos adicionados ao carrinho.', 'success')

    except Exception as e:
        db.session.rollback()
        print(f"[ERRO ADICIONAR CARRINHO]: {str(e)}")
        flash('Erro ao reservar os ingressos.', 'danger')

    return redirect(url_for('ver_carrinho'))

@app.route('/carrinho/remover/<int:lote_id>', methods=['POST'])
def remover_carrinho(lote_id):
    session_id = session.get('session_token')
    if session_id:
        ReservaCarrinho.query.filter_by(session_id=session_id, lote_id=lote_id).delete(synchronize_session=False)
        db.session.commit()

    carrinho = session.get('carrinho', {})
    if str(lote_id) in carrinho:
        del carrinho[str(lote_id)]
        session['carrinho'] = carrinho
        session.modified = True
        
    return redirect(url_for('ver_carrinho'))

@app.route('/carrinho/limpar', methods=['POST'])
def limpar_carrinho():
    session_id = session.get('session_token')
    if session_id:
        ReservaCarrinho.query.filter_by(session_id=session_id).delete(synchronize_session=False)
        db.session.commit()

    session.pop('carrinho', None)
    return redirect(url_for('ver_carrinho'))

# ==========================================================================
# WEBHOOK HÍBRIDO (ORDERS API & PAYMENTS API)
# ==========================================================================

@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    if not validar_assinatura_mercadopago(request):
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    topic = data.get("type") or request.args.get("topic")
    resource_id = data.get("data", {}).get("id") or request.args.get("id")

    if not resource_id:
        return jsonify({"status": "ignored"}), 200

    try:
        if topic in ["payment", "payment.created", "payment.updated"] and sdk:
            payment_info = sdk.payment().get(resource_id)
            if payment_info.get("status") == 200:
                p_data = payment_info["response"]
                p_status = p_data.get("status")
                ext_ref = p_data.get("external_reference", "")

                if ext_ref.startswith("PEDIDO_"):
                    pedido_id = int(ext_ref.split("_")[1])

                    if p_status in ["approved", "accredited"]:
                        gerar_ingressos_para_pedido(pedido_id, resource_id)
                    elif p_status in ["cancelled", "refunded", "rejected"]:
                        Ingresso.query.filter_by(pedido_id=pedido_id).delete()
                        Pedido.query.filter_by(id=pedido_id).update({'status': p_status})
                        db.session.commit()

        elif topic in ["order", "merchant_order"] and orders_api:
            order_info, status_code = orders_api.get_order(resource_id)
            if status_code == 200:
                order_status = order_info.get("status")
                ext_ref = order_info.get("external_reference", "")

                if ext_ref.startswith("PEDIDO_"):
                    pedido_id = int(ext_ref.split("_")[1])

                    if order_status in ["processed", "accredited"]:
                        gerar_ingressos_para_pedido(pedido_id, resource_id)
                    elif order_status in ["cancelled", "refunded", "rejected"]:
                        Ingresso.query.filter_by(pedido_id=pedido_id).delete()
                        Pedido.query.filter_by(id=pedido_id).update({'status': order_status})
                        db.session.commit()

    except Exception as e:
        db.session.rollback()
        print(f"[ERRO WEBHOOK MP]: {str(e)}")

    return jsonify({"status": "ok"}), 200

# ==========================================================================
# ROTAS AUTENTICADAS DO CLIENTE E CHECKOUT PÚBLICO
# ==========================================================================

@app.route('/meus-ingressos')
@cliente_required
def meus_ingressos():
    ingressos = Ingresso.query.filter_by(usuario_id=session.get('usuario_id')).order_by(Ingresso.data_compra.desc()).all()
    return render_template('meus_ingressos.html', vendas=ingressos)

@app.route('/perfil')
@cliente_required
def perfil():
    usuario = Usuario.query.get(session['usuario_id'])
    if not usuario:
        session.clear()
        return redirect(url_for('login'))
    return render_template('perfil.html', usuario=usuario)

@app.route('/editar-perfil', methods=['GET', 'POST'])
@cliente_required
def editar_perfil():
    usuario = Usuario.query.get(session['usuario_id'])
    if request.method == 'POST':
        usuario.nome = request.form.get('nome', '').strip()
        usuario.cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
        usuario.telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
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
        usuario.nome = request.form.get('nome', '').strip()
        usuario.telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
        db.session.commit()
        flash('Configurações salvas.', 'success')
        return redirect(url_for('configuracoes'))

    return render_template('configuracoes.html', usuario=usuario)

@app.route('/configuracoes/alterar-senha', methods=['POST'])
@cliente_required
def alterar_senha_configuracoes():
    usuario = Usuario.query.get(session['usuario_id'])
    senha_atual = request.form.get('senha_atual', '')
    nova_senha = request.form.get('nova_senha', '')

    if check_password_hash(usuario.senha_hash, senha_atual) and len(nova_senha) >= 6:
        usuario.senha_hash = generate_password_hash(nova_senha)
        db.session.commit()
        flash('Senha alterada!', 'success')
    else:
        flash('Erro ao redefinir a senha.', 'danger')

    return redirect(url_for('configuracoes'))

@app.route('/deletar-conta', methods=['POST'])
@cliente_required
def deletar_conta():
    usuario = Usuario.query.get(session['usuario_id'])
    if check_password_hash(usuario.senha_hash, request.form.get('confirm_senha', '')):
        db.session.delete(usuario)
        db.session.commit()
        session.clear()
        return redirect(url_for('login'))
    
    flash('Senha incorreta.', 'danger')
    return redirect(url_for('configuracoes'))

@app.route('/meus-favoritos')
@cliente_required
def meus_favoritos():
    return render_template('favoritos.html')

@app.route('/favoritar', methods=['POST'])
def favoritar():
    if 'usuario_id' not in session:
        return jsonify({'status': 'error', 'message': 'Autenticação necessária'}), 401

    data = request.get_json(silent=True) or {}
    evento_id_raw = str(data.get('evento_id', '')).strip()

    favoritos = session.get('favoritos', [])
    item_existente = next((i for i in favoritos if str(i.get('id')) == evento_id_raw), None)

    if item_existente:
        favoritos.remove(item_existente)
        favoritado = False
    else:
        favoritos.append({'id': evento_id_raw, 'nome': f'Evento {evento_id_raw}'})
        favoritado = True

    session['favoritos'] = favoritos
    session.modified = True
    
    return jsonify({
        'status': 'success', 
        'favoritado': favoritado,
        'total_favoritos': len(favoritos)
    })

# ==========================================================================
# CHECKOUT DESMEMBRADO (PIX VIA ORDERS API / CARTÃO VIA ORDERS API)
# ==========================================================================

@app.route('/checkout', methods=['GET', 'POST'])
@cliente_required
def checkout():
    usuario_atual = Usuario.query.get(session['usuario_id'])
    carrinho = session.get('carrinho', {})

    if not carrinho or vendas_encerradas():
        return redirect(url_for('evento_marevibes'))

    ordem_compra = []
    total_pedido = Decimal('0.00')

    for item_data in carrinho.values():
        lote_obj = Lote.query.get(item_data.get('lote_id'))
        if lote_obj:
            lote_preco_dec = Decimal(str(lote_obj.preco))
            qtd = item_data.get('quantidade', 0)
            subtotal = qtd * lote_preco_dec
            total_pedido += subtotal
            
            ordem_compra.append({
                'lote': lote_obj,
                'quantidade': qtd,
                'preco_unitario': lote_preco_dec
            })

    if request.method == 'POST':
        if not orders_api:
            flash('Sistema de pagamentos temporariamente indisponível.', 'danger')
            return redirect(url_for('checkout'))

        metodo = request.form.get('metodo_pagamento', 'pix')
        installments = converter_int_seguro(request.form.get('installments'), padrao=1)
        if installments not in [1, 2]:
            installments = 1

        calc_taxa = calcular_valor_com_taxa_mp(total_pedido, metodo_pagamento=metodo, parcelas=installments)
        valor_final_dec = calc_taxa['valor_final']
        valor_final_float = float(valor_final_dec)

        # 1. Montagem da lista de itens desmembrando ingresso e taxas
        items_payments_payload = []
        for item in ordem_compra:
            items_payments_payload.append({
                "title": f"Ingresso {item['lote'].nome}",
                "quantity": item['quantidade'],
                "unit_price": float(item['preco_unitario'])
            })

        if calc_taxa['taxa'] > Decimal('0.00'):
            items_payments_payload.append({
                "title": "Taxa de Processamento / Serviços",
                "quantity": 1,
                "unit_price": float(calc_taxa['taxa'])
            })

        # 2. Criação do Pedido no Banco Local
        novo_pedido = None
        try:
            novo_pedido = Pedido(
                usuario_id=usuario_atual.id,
                total=valor_final_dec,
                status='pending',
                metodo_pagamento=metodo
            )
            db.session.add(novo_pedido)
            db.session.flush()

            for item in ordem_compra:
                db.session.add(ItemPedido(
                    pedido_id=novo_pedido.id,
                    lote_id=item['lote'].id,
                    quantidade=item['quantidade'],
                    preco_unitario=item['preco_unitario']
                ))

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[ERRO CRIACAO PEDIDO]: {str(e)}")
            flash('Erro ao gerar o pedido. Tente novamente.', 'danger')
            return redirect(url_for('checkout'))

        try:
            partes_nome = usuario_atual.nome.strip().split(' ', 1) if usuario_atual.nome else ["Cliente", ""]
            first_name = partes_nome[0]
            last_name = partes_nome[1] if len(partes_nome) > 1 and partes_nome[1] else "MaréVibes"
            cpf_limpo = re.sub(r'\D', '', usuario_atual.cpf) if usuario_atual.cpf else ""

            # 3. Construção do Payment Method
            if metodo == 'pix':
                payment_method_object = {
                    "id": "pix",
                    "type": "bank_transfer"
                }
            elif metodo == 'credit_card':
                payment_method_id = request.form.get('payment_method_id', '')
                card_token = request.form.get('token')

                if not card_token:
                    novo_pedido.status = 'failed'
                    db.session.commit()
                    flash('Falha ao processar dados do cartão. Tente novamente.', 'warning')
                    return redirect(url_for('checkout'))

                payment_method_object = {
                    "id": payment_method_id,
                    "type": "credit_card",
                    "token": card_token,
                    "installments": installments
                }

            # 4. Payload com modelo AUTOMATIC (One-Shot)
            order_payload = {
                "type": "online",
                "processing_mode": "automatic",
                "external_reference": f"PEDIDO_{novo_pedido.id}",
                "total_amount": valor_final_float,
                "payer": {
                    "email": usuario_atual.email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "identification": {
                        "type": "CPF",
                        "number": cpf_limpo
                    }
                },
                "items": items_payments_payload,
                "transactions": {
                    "payments": [
                        {
                            "amount": valor_final_float,
                            "payment_method": payment_method_object
                        }
                    ]
                }
            }

            # Executa a requisição única de criação e processamento
            res_order, status_order = orders_api.create_order(order_payload)

            if status_order not in [200, 201]:
                print(f"[ERRO ORDERS API AUTOMATIC]: Code {status_order} - {res_order}")
                novo_pedido.status = 'failed'
                db.session.commit()
                flash('Não foi possível processar a ordem de pagamento. Tente novamente.', 'danger')
                return redirect(url_for('checkout'))

            order_id = res_order.get("id")
            order_status = res_order.get("status")
            payments = res_order.get("transactions", {}).get("payments", [])
            primary_payment = payments[0] if payments else {}
            payment_status = primary_payment.get("status")
            status_detail = primary_payment.get("status_detail", "")

            # 5. Tratamento para PIX
            if metodo == 'pix':
                novo_pedido.pagamento_id = str(order_id)
                db.session.commit()

                pix_info = primary_payment.get("point_of_interaction", {}).get("transaction_data", {})

                session['compra_atual'] = {
                    'metodo_pagamento': 'pix',
                    'payment_id': str(order_id),
                    'pedido_id': novo_pedido.id,
                    'total': float(valor_final_dec),
                    'qr_code': pix_info.get("qr_code"),
                    'qr_code_base64': pix_info.get("qr_code_base64")
                }
                session.pop('carrinho', None)
                return redirect(url_for('pagamento'))

            # 6. Tratamento para CARTÃO DE CRÉDITO
            elif metodo == 'credit_card':
                if order_status in ["processed", "accredited"] or payment_status in ["approved", "accredited"]:
                    novo_pedido.status = "approved"
                    novo_pedido.pagamento_id = str(order_id)
                    gerar_ingressos_para_pedido(novo_pedido.id, order_id)
                    db.session.commit()
                    session.pop('carrinho', None)
                    flash('Pagamento processado com sucesso!', 'success')
                    return redirect(url_for('meus_ingressos'))
                else:
                    novo_pedido.status = payment_status or 'rejected'
                    db.session.commit()
                    msg_erro = MENSAGENS_ERRO_MP.get(
                        status_detail,
                        f'Pagamento recusado ({status_detail or "motivo desconhecido"}). Verifique os dados ou utilize outro cartão.'
                    )
                    flash(f"Falha no pagamento: {msg_erro}", "danger")
                    return redirect(url_for("checkout"))

        except Exception as e:
            print(f"[ERRO MERCADO PAGO EXCECAO]: {str(e)}")
            if novo_pedido:
                novo_pedido.status = 'failed'
                db.session.commit()
            flash('Erro técnico ao processar o pagamento. Tente novamente.', 'danger')
            return redirect(url_for('checkout'))

    return render_template('checkout.html', usuario=usuario_atual, ordem_compra=ordem_compra, total_pedido=total_pedido)

@app.route('/pagamento')
@cliente_required
def pagamento():
    compra = session.get('compra_atual')
    if not compra:
        return redirect(url_for('index'))
    return render_template('pagamento.html', compra=compra)

@app.route('/api/checar-status-pagamento/<payment_id>')
@cliente_required
def checar_status_pagamento(payment_id):
    try:
        if sdk:
            payment_info = sdk.payment().get(payment_id)
            if payment_info.get("status") == 200:
                res = payment_info["response"]
                p_status = res.get("status")
                if p_status in ['approved', 'accredited']:
                    ext_ref = res.get("external_reference", "")
                    if ext_ref and ext_ref.startswith("PEDIDO_"):
                        try:
                            pedido_id = int(ext_ref.split("_")[1])
                            gerar_ingressos_para_pedido(pedido_id, payment_id)
                        except (ValueError, IndexError):
                            pass

                    if 'compra_atual' in session:
                        session['compra_atual']['status'] = 'approved'
                        session.modified = True

                    return jsonify({'status': 'approved', 'redirect_url': url_for('meus_ingressos')})
                return jsonify({'status': p_status})

        if orders_api:
            res, status_code = orders_api.get_order(payment_id)
            if status_code == 200:
                order_status = res.get("status")
                payments = res.get("transactions", {}).get("payments", [])
                primary_payment = payments[0] if payments else {}
                payment_status = primary_payment.get("status") or order_status

                if order_status in ['processed', 'accredited'] or payment_status in ['processed', 'accredited', 'approved']:
                    ext_ref = res.get("external_reference", "")
                    if ext_ref and ext_ref.startswith("PEDIDO_"):
                        try:
                            pedido_id = int(ext_ref.split("_")[1])
                            gerar_ingressos_para_pedido(pedido_id, payment_id)
                        except (ValueError, IndexError):
                            pass

                    if 'compra_atual' in session:
                        session['compra_atual']['status'] = 'approved'
                        session.modified = True

                    return jsonify({'status': 'approved', 'redirect_url': url_for('meus_ingressos')})

                return jsonify({'status': payment_status})

        return jsonify({'status': 'error', 'message': 'Pagamento não encontrado'}), 404

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

# ==========================================================================
# ROTAS ADMINISTRATIVAS
# ==========================================================================

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    lotes = Lote.query.all()
    vendidos = Ingresso.query.count()
    utilizados = Ingresso.query.filter_by(status='utilizado').count()

    receita = db.session.query(func.sum(Lote.preco)).join(Ingresso, Ingresso.lote_id == Lote.id).scalar() or Decimal('0.00')

    stats = {
        'ingressos_vendidos': vendidos,
        'ingressos_utilizados': utilizados,
        'receita_total': receita
    }
    return render_template('admin/dashboard.html', stats=stats, lotes=lotes)

@app.route('/admin/trocar-lote/<int:lote_id>')
@admin_required
def trocar_lote_ativo(lote_id):
    Lote.query.update({Lote.ativo: False})
    lote_alvo = Lote.query.get_or_404(lote_id)
    lote_alvo.ativo = True
    db.session.commit()
    flash(f'Lote alterado para: {lote_alvo.nome}', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/validar', methods=['GET', 'POST'])
@admin_required
def painel_validacao():
    resultado = None
    if request.method == 'POST':
        codigo = request.form.get('codigo', '').strip()
        ingresso = Ingresso.query.filter_by(codigo_qr=codigo).first()
        if ingresso and request.form.get('acao') == 'dar_baixa':
            ingresso.status = 'utilizado'
            ingresso.data_uso = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()
            flash('Entrada liberada!', 'success')
        resultado = ingresso
    return render_template('admin/validar.html', resultado=resultado)

# --------------------------------------------------------------------------
# Comando CLI para inicialização manual
# --------------------------------------------------------------------------
@app.cli.command('init-db')
def init_db_command():
    inicializar_banco()
    print("Banco de dados inicializado com sucesso!")

if __name__ == '__main__':
    app.run(debug=False)
