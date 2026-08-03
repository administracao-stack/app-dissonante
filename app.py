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

# --- Configurações do Servidor SMTP (Google Workspace / Gmail) ---
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
mail_port = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_PORT'] = mail_port

# Previne conflito entre SSL e TLS
if mail_port == 465:
    app.config['MAIL_USE_SSL'] = True
    app.config['MAIL_USE_TLS'] = False
else:
    app.config['MAIL_USE_SSL'] = False
    app.config['MAIL_USE_TLS'] = True

app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_TIMEOUT'] = 10

mail = Mail(app)

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

def disparar_email_async(app_obj, msg):
    with app_obj.app_context():
        try:
            mail.send(msg)
            print(f"[E-MAIL ENVIADO COM SUCESSO]: Para {msg.recipients}")
        except Exception as e:
            print(f"[ERRO DISPARO E-MAIL ASYNC]: {str(e)}")

def enviar_email_confirmacao(usuario_email, usuario_nome, token):
    try:
        link_validacao = url_for('validar_email', token=token, _external=True)
        
        msg = Message(
            subject="[Dissonante Experiências] Validação do seu E-mail",
            recipients=[usuario_email]
        )

        msg.body = f"""Olá, {usuario_nome}!

Seja bem-vindo(a) à Dissonante Experiências.

Para ativar a sua conta e garantir o acesso aos seus ingressos, clique no link de confirmação abaixo:
{link_validacao}

Atenção: Este link expirará em 24 horas.

Atenciosamente,
Equipe Dissonante Experiências
"""
        # Passa 'app' diretamente para a thread (CORRIGIDO)
        thread = threading.Thread(
            target=disparar_email_async, 
            args=(app, msg)
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

# --------------------------------------------------------------------------
# Rotas Públicas
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

@app.route('/contato', methods=['GET', 'POST'])
def contato():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email_cliente = request.form.get('email', '').strip()
        assunto = request.form.get('assunto', '').strip()
        mensagem = request.form.get('mensagem', '').strip()

        email_empresa = app.config['MAIL_USERNAME']

        if not email_empresa:
            flash('Serviço de e-mail indisponível no momento. Tente contato via WhatsApp.', 'warning')
            return redirect(url_for('contato'))

        msg = Message(
            subject=f"[Contato via Site] {assunto}",
            recipients=[email_empresa],
            reply_to=email_cliente
        )

        msg.body = f"""
Nova mensagem de contato recebida pelo site:

Nome: {nome}
E-mail do Cliente: {email_cliente}
Assunto: {assunto}

Mensagem:
--------------------------------------------------
{mensagem}
--------------------------------------------------
        """

        # Passa 'app' diretamente para a thread (CORRIGIDO)
        thread = threading.Thread(
            target=disparar_email_async, 
            args=(app, msg)
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
            # Passando 'novo_usuario.nome' corretamente (CORRIGIDO)
            enviar_email_confirmacao(novo_usuario.email, novo_usuario.nome, token)

            flash('Cadastro realizado com sucesso! Enviamos um e-mail de ativação para você. Verifique sua caixa de entrada e spam.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO NO CADASTRO]: {str(e)}")
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

# --------------------------------------------------------------------------
# Checkout e Ingressos
# --------------------------------------------------------------------------

@app.route('/checkout', methods=['GET', 'POST'])
@cliente_required
def checkout():
    lote_id_req = request.form.get('lote_id', type=int) or request.args.get('lote_id', type=int)
    
    if lote_id_req:
        lote_ativo = Lote.query.get(lote_id_req)
    else:
        lote_ativo = Lote.query.filter_by(ativo=True).first()

    if not lote_ativo:
        flash('Nenhum lote de ingressos disponível no momento.', 'warning')
        return redirect(url_for('evento_marevibes'))

    usuario_id = session.get('usuario_id')
    usuario_atual = Usuario.query.get(usuario_id) if usuario_id else None

    if not usuario_atual:
        session.clear()
        flash('Sua sessão expirou. Por favor, faça login novamente.', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            quantidade = max(1, int(request.form.get('quantidade', 1)))
        except (ValueError, TypeError):
            quantidade = 1

        metodo_pagamento = request.form.get('metodo_pagamento', 'pix')

        ingressos_vendidos_lote = Ingresso.query.filter_by(lote_id=lote_ativo.id).count()
        disponiveis_lote = lote_ativo.quantidade_total - ingressos_vendidos_lote

        if quantidade > disponiveis_lote:
            flash(f'Restam apenas {disponiveis_lote} ingresso(s) no {lote_ativo.nome}.', 'danger')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

        total = quantidade * lote_ativo.preco

        nome_completo = (usuario_atual.nome or 'Cliente').strip().split(' ', 1)
        first_name = nome_completo[0]
        last_name = nome_completo[1] if len(nome_completo) > 1 else "Silva"

        cpf_usuario = re.sub(r'\D', '', usuario_atual.cpf or '')
        ddd_tel, num_tel = extrair_ddd_e_numero(usuario_atual.telefone)

        if len(cpf_usuario) != 11:
            flash('É necessário possuir um CPF válido cadastrado na conta para concluir a compra.', 'danger')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

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
                flash('Falha de conexão com o Mercado Pago. Tente novamente.', 'danger')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

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
                flash('Falha na comunicação com a operadora do cartão.', 'danger')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

    return render_template('checkout.html', lote=lote_ativo)

@app.route('/pagamento')
@cliente_required
def pagamento():
    compra = session.get('compra_atual')
    if not compra:
        flash('Nenhuma transação ativa encontrada.', 'info')
        return redirect(url_for('checkout'))
    return render_template('pagamento.html', compra=compra)

@app.route('/api/checar-status-pagamento/<payment_id>')
@cliente_required
def checar_status_pagamento(payment_id):
    if not sdk:
        return jsonify({"status": "pending"})

    try:
        payment_response = sdk.payment().get(payment_id)
        payment_info = payment_response.get("response", {})
        status = payment_info.get("status")

        if status == "approved":
            ext_ref = payment_info.get("external_reference", "")
            user_id, lote_id, qtd = extrair_ref_externa(ext_ref)
            if user_id and lote_id and qtd:
                gerar_ingressos_para_pagamento(payment_id, user_id, lote_id, qtd)

            session.pop('compra_atual', None)
            return jsonify({"status": "approved", "redirect_url": url_for('meus_ingressos')})

        return jsonify({"status": status or "pending"})
    except Exception as e:
        print(f"[ERRO POLLING PAGAMENTO]: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    if not sdk:
        return jsonify({"status": "ok"}), 200

    data = request.get_json() or {}
    if data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        
        try:
            payment_info = sdk.payment().get(payment_id).get("response", {})
            
            if payment_info.get("status") == "approved":
                ext_ref = payment_info.get("external_reference", "")
                user_id, lote_id, qtd = extrair_ref_externa(ext_ref)
                
                if user_id and lote_id and qtd:
                    gerar_ingressos_para_pagamento(payment_id, user_id, lote_id, qtd)
                else:
                    payer_email = payment_info.get("payer", {}).get("email")
                    usuario = Usuario.query.filter_by(email=payer_email).first()
                    lote = Lote.query.filter_by(ativo=True).first()
                    if usuario and lote:
                        gerar_ingressos_para_pagamento(payment_id, usuario.id, lote.id, 1)

        except Exception as e:
            db.session.rollback()
            print(f"[ERRO WEBHOOK MERCADO PAGO]: {str(e)}")

    return jsonify({"status": "ok"}), 200

@app.route('/meus-ingressos')
@cliente_required
def meus_ingressos():
    ingressos_db = Ingresso.query.filter_by(usuario_id=session['usuario_id']).order_by(Ingresso.data_compra.desc()).all()
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
    app.run(debug=True)
