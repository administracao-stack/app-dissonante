import os
import re
import random
import string
import threading
from datetime import datetime, timezone, timedelta
from functools import wraps
from dotenv import load_dotenv
import mercadopago
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

# Carrega as variáveis de ambiente
load_dotenv()

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configurações do App, Banco de Dados e E-mail
# --------------------------------------------------------------------------
app.secret_key = os.environ.get('SECRET_KEY', 'chave_secreta_para_desenvolvimento')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# --- Configurações de E-mail Ajustadas para Evitar Conflitos de SSL/TLS ---
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
mail_port = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_PORT'] = mail_port

# Previne o conflito: Porta 465 usa SSL, Porta 587 usa TLS
if mail_port == 465:
    app.config['MAIL_USE_SSL'] = True
    app.config['MAIL_USE_TLS'] = False
else:
    app.config['MAIL_USE_SSL'] = False
    app.config['MAIL_USE_TLS'] = True

app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_TIMEOUT'] = 10  # Previne travamentos na conexão SMTP

mail = Mail(app)

# URL do Banco de Dados com Tratamento SSL para o Render
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
            lote_promo = Lote.query.filter(Lote.nome.ilike('%promocional%')).first()
            if not lote_promo:
                lote_promo = Lote(nome='Lote Promocional', preco=160.00, quantidade_total=15, ativo=True)
                db.session.add(lote_promo)
            else:
                lote_promo.preco = 160.00
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
# Funções Auxiliares e Envio Assíncrono
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

def disparar_email_async(app_obj, msg):
    with app_obj.app_context():
        try:
            mail.send(msg)
            print(f"[E-MAIL ENVIADO]: Para {msg.recipients}")
        except Exception as e:
            print(f"[ERRO NO ENVIO DE E-MAIL]: {str(e)}")

def enviar_email_confirmacao(usuario_email, usuario_nome, token):
    link_validacao = url_for('validar_email', token=token, _external=True)
    
    msg = Message(
        subject="[Dissonante Experiências] Validação do seu E-mail",
        recipients=[usuario_email]
    )

    msg.body = f"""Olá, {usuario_nome}!

Seja bem-vindo(a) à Dissonante Experiências.

Para ativar a sua conta, clique no link de confirmação abaixo:
{link_validacao}

Atenção: Este link expirará em 24 horas.

Atenciosamente,
Equipe Dissonante Experiências
"""
    thread = threading.Thread(
        target=disparar_email_async, 
        args=(app._get_current_object(), msg)
    )
    thread.start()
    return True

# --------------------------------------------------------------------------
# Rotas
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

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        try:
            nome = request.form.get('nome', '').strip()
            email = request.form.get('email', '').strip().lower()
            cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
            telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
            senha = request.form.get('senha')
            confirmar = request.form.get('confirmar_senha')

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

            # Envio de e-mail em background
            token = gerar_token_confirmacao(novo_usuario.email)
            enviar_email_confirmacao(novo_usuario.email, novo_usuario.nome, token)

            flash('Cadastro realizado com sucesso! Enviamos um e-mail de ativação para você. Verifique sua caixa de entrada e spam.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO NO CADASTRO]: {str(e)}")
            flash(f'Erro ao realizar cadastro: {str(e)}', 'danger')
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
        senha = request.form.get('senha')

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
            return redirect(url_for('meus_ingressos'))
        else:
            flash('E-mail ou senha incorretos.', 'danger')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Sessão encerrada com sucesso.', 'info')
    return redirect(url_for('login'))

# Inicializa banco de dados
inicializar_banco()

if __name__ == '__main__':
    app.run(debug=True)
