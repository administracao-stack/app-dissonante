import os
import re
import random
import string
import mercadopago
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configurações do App e Banco de Dados (PostgreSQL / Render)
# --------------------------------------------------------------------------
app.secret_key = os.environ.get('SECRET_KEY', 'chave_secreta_para_desenvolvimento')

# Pega a DATABASE_URL do Render (ou do .env local)
database_url = os.environ.get('DATABASE_URL')

# Correção automática para o SQLAlchemy se a URL começar com 'postgres://'
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --------------------------------------------------------------------------
# Configuração do Mercado Pago
# --------------------------------------------------------------------------
MERCADOPAGO_TOKEN = os.environ.get('MP_ACCESS_TOKEN', 'SEU_ACCESS_TOKEN_DO_MERCADO_PAGO')
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)

# --------------------------------------------------------------------------
# Modelos do Banco de Dados (ORM SQLAlchemy)
# --------------------------------------------------------------------------

class Usuario(db.Model):
    __tablename__ = 'usuarios'
    
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    cpf = db.Column(db.String(14), nullable=True)        # CPF do cliente
    telefone = db.Column(db.String(20), nullable=True)   # Telefone/WhatsApp do cliente
    senha_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)

    ingressos = db.relationship('Ingresso', backref='comprador', lazy=True)


class Lote(db.Model):
    __tablename__ = 'lotes'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(50), nullable=False)  # Ex: 'Lote Promocional', '1º Lote'
    preco = db.Column(db.Float, nullable=False)
    quantidade_total = db.Column(db.Integer, nullable=False)
    ativo = db.Column(db.Boolean, default=False)
    data_inicio = db.Column(db.DateTime, default=datetime.utcnow)

    ingressos = db.relationship('Ingresso', backref='lote_origem', lazy=True)


class Ingresso(db.Model):
    __tablename__ = 'ingressos'

    id = db.Column(db.Integer, primary_key=True)
    codigo_qr = db.Column(db.String(50), unique=True, nullable=False)
    evento_nome = db.Column(db.String(100), nullable=False, default="MaréVibes Halloween 2026")
    status = db.Column(db.String(20), default='valido')  # 'valido', 'utilizado', 'cancelado'
    data_compra = db.Column(db.DateTime, default=datetime.utcnow)
    data_uso = db.Column(db.DateTime, nullable=True)
    
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    lote_id = db.Column(db.Integer, db.ForeignKey('lotes.id'), nullable=True)
    pagamento_id = db.Column(db.String(100), nullable=True)


with app.app_context():
    db.create_all()
    if not Lote.query.first():
        lote_inicial = Lote(nome='Lote Promocional', preco=35.00, quantidade_total=100, ativo=True)
        db.session.add(lote_inicial)
        db.session.commit()

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

def gerar_codigo_ingresso():
    hash_aleatorio = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    return f"DISSONANTE-HLW-{hash_aleatorio}"

def extrair_ddd_e_numero(telefone_raw):
    """Extrai DDD (2 dígitos) e número de telefone limpando caracteres."""
    numeros = re.sub(r'\D', '', str(telefone_raw or ''))
    if len(numeros) >= 10:
        return numeros[:2], numeros[2:]
    return "85", numeros if numeros else "999999999"

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
    lote_ativo = Lote.query.filter_by(ativo=True).first()
    return render_template('evento_marevibes.html', lote=lote_ativo)

@app.route('/termos-de-uso')
def termos_de_uso():
    return render_template('termos_de_uso.html')

@app.route('/politica-de-privacidade')
def politica_privacidade():
    return render_template('politica_privacidade.html')

@app.route('/contato', methods=['GET', 'POST'])
def contato():
    if request.method == 'POST':
        flash('Mensagem enviada com sucesso! Em breve entraremos em contato.', 'success')
        return redirect(url_for('contato'))
    return render_template('contato.html')

# --------------------------------------------------------------------------
# Autenticação
# --------------------------------------------------------------------------

@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if request.method == 'POST':
        nome = request.form.get('nome', '').strip()
        email = request.form.get('email', '').strip().lower()
        cpf = re.sub(r'\D', '', request.form.get('cpf', ''))
        telefone = re.sub(r'\D', '', request.form.get('telefone', ''))
        senha = request.form.get('senha')
        confirmar = request.form.get('confirmar_senha')

        if senha != confirmar:
            flash('As senhas digitadas não coincidem.', 'danger')
            return redirect(url_for('cadastro'))

        if Usuario.query.filter_by(email=email).first():
            flash('Este e-mail já está cadastrado. Faça login para continuar.', 'warning')
            return redirect(url_for('login'))

        hash_senha = generate_password_hash(senha)
        novo_usuario = Usuario(
            nome=nome,
            email=email,
            cpf=cpf,
            telefone=telefone,
            senha_hash=hash_senha
        )
        
        db.session.add(novo_usuario)
        db.session.commit()

        flash('Conta criada com sucesso! Faça login abaixo.', 'success')
        return redirect(url_for('login'))

    return render_template('cadastro.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        senha = request.form.get('senha')

        usuario = Usuario.query.filter_by(email=email).first()

        if usuario and check_password_hash(usuario.senha_hash, senha):
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

# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------

@app.route('/checkout', methods=['GET', 'POST'])
@cliente_required
def checkout():
    lote_ativo = Lote.query.filter_by(ativo=True).first()

    if not lote_ativo:
        flash('Nenhum lote de ingressos disponível no momento.', 'warning')
        return redirect(url_for('evento_marevibes'))

    if request.method == 'POST':
        quantidade = int(request.form.get('quantidade', 1))
        metodo_pagamento = request.form.get('metodo_pagamento', 'pix')
        
        # Trava de Regra de Negócio: Lote Promocional aceita APENAS Pix
        if 'promocional' in lote_ativo.nome.lower() and metodo_pagamento != 'pix':
            flash('O Lote Promocional aceita apenas pagamento via Pix.', 'warning')
            return redirect(url_for('checkout'))

        # Validação de estoque
        ingressos_vendidos_lote = Ingresso.query.filter_by(lote_id=lote_ativo.id).count()
        disponiveis_lote = lote_ativo.quantidade_total - ingressos_vendidos_lote

        if quantidade > disponiveis_lote:
            flash(f'Restam apenas {disponiveis_lote} ingresso(s) no {lote_ativo.nome}.', 'danger')
            return redirect(url_for('checkout'))

        preco_unitario = lote_ativo.preco
        total = quantidade * preco_unitario

        # Busca dados atualizados do usuário no BD para montar a requisição do Mercado Pago
        usuario_atual = Usuario.query.get(session['usuario_id'])

        nome_completo = (usuario_atual.nome if usuario_atual else session.get('usuario_nome', 'Cliente')).strip().split(' ', 1)
        first_name = nome_completo[0]
        last_name = nome_completo[1] if len(nome_completo) > 1 else "Silva"

        cpf_usuario = re.sub(r'\D', '', usuario_atual.cpf) if usuario_atual and usuario_atual.cpf else ''
        ddd_tel, num_tel = extrair_ddd_e_numero(usuario_atual.telefone if usuario_atual else '')

        # Payload de pagador completo para o Mercado Pago
        payer_payload = {
            "email": session['usuario_email'],
            "first_name": first_name,
            "last_name": last_name,
            "identification": {
                "type": "CPF",
                "number": cpf_usuario
            },
            "phone": {
                "area_code": ddd_tel,
                "number": num_tel
            }
        }

        # --- PAGAMENTO VIA PIX ---
        if metodo_pagamento == 'pix':
            payment_data = {
                "transaction_amount": float(total),
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes",
                "payment_method_id": "pix",
                "payer": payer_payload
            }

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})

                if payment.get("status") in ["pending", "approved"]:
                    pix_info = payment["point_of_interaction"]["transaction_data"]
                    session['compra_atual'] = {
                        'metodo_pagamento': 'pix',
                        'payment_id': payment["id"],
                        'lote_id': lote_ativo.id,
                        'total': total,
                        'quantidade': quantidade,
                        'qr_code': pix_info["qr_code"],
                        'qr_code_base64': pix_info["qr_code_base64"]
                    }
                    return redirect(url_for('pagamento'))
                else:
                    print("Erro no Mercado Pago (PIX):", payment)
                    flash('Erro ao gerar cobrança Pix. Tente novamente.', 'danger')
            except Exception as e:
                print("Exceção ao criar PIX:", str(e))
                flash('Falha na comunicação com o Mercado Pago.', 'danger')

        # --- PAGAMENTO VIA CARTÃO DE CRÉDITO ---
        elif metodo_pagamento == 'credit_card':
            token = request.form.get('token')
            installments = int(request.form.get('installments', 1))
            payment_method_id = request.form.get('payment_method_id')

            # Trava no backend limitando parcelas a no máximo 2x
            if installments > 2:
                installments = 2

            payment_data = {
                "transaction_amount": float(total),
                "token": token,
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes",
                "installments": installments,
                "payment_method_id": payment_method_id,
                "payer": payer_payload
            }

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status = payment.get("status")

                if status == "approved":
                    for _ in range(quantidade):
                        novo_ingresso = Ingresso(
                            codigo_qr=gerar_codigo_ingresso(),
                            usuario_id=session['usuario_id'],
                            lote_id=lote_ativo.id,
                            pagamento_id=str(payment.get("id"))
                        )
                        db.session.add(novo_ingresso)
                    db.session.commit()

                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'approved',
                        'payment_id': payment.get("id"),
                        'lote_id': lote_ativo.id,
                        'total': total,
                        'quantidade': quantidade
                    }

                    flash('Pagamento aprovado com sucesso! Seus ingressos foram gerados.', 'success')
                    return redirect(url_for('pagamento'))
                
                elif status == "in_process":
                    session['compra_atual'] = {
                        'metodo_pagamento': 'credit_card',
                        'status': 'in_process',
                        'payment_id': payment.get("id"),
                        'lote_id': lote_ativo.id,
                        'total': total,
                        'quantidade': quantidade
                    }
                    flash('Pagamento em análise pelo seu banco. Acompanhe o status nesta página ou em Meus Ingressos.', 'info')
                    return redirect(url_for('pagamento'))
                else:
                    print("Erro no Mercado Pago (Cartão):", payment)
                    flash('Cartão recusado ou dados incorretos. Tente novamente.', 'danger')
            except Exception as e:
                print("Exceção ao processar Cartão:", str(e))
                flash('Falha na comunicação com a operadora do cartão.', 'danger')

    return render_template('checkout.html', lote=lote_ativo)

@app.route('/pagamento')
@cliente_required
def pagamento():
    compra = session.get('compra_atual')
    if not compra:
        return redirect(url_for('checkout'))
    return render_template('pagamento.html', compra=compra)

# Webhook Mercado Pago
@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    data = request.get_json() or {}
    if data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        payment_info = sdk.payment().get(payment_id).get("response", {})
        
        if payment_info.get("status") == "approved":
            if not Ingresso.query.filter_by(pagamento_id=str(payment_id)).first():
                payer_email = payment_info.get("payer", {}).get("email")
                usuario = Usuario.query.filter_by(email=payer_email).first()
                lote_ativo = Lote.query.filter_by(ativo=True).first()
                
                if usuario and lote_ativo:
                    valor_total = payment_info.get("transaction_amount", lote_ativo.preco)
                    qtd = int(valor_total // lote_ativo.preco)
                    
                    for _ in range(qtd):
                        novo_ingresso = Ingresso(
                            codigo_qr=gerar_codigo_ingresso(),
                            usuario_id=usuario.id,
                            lote_id=lote_ativo.id,
                            pagamento_id=str(payment_id)
                        )
                        db.session.add(novo_ingresso)
                    db.session.commit()

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
    receita_total = sum(i.lote_origem.preco for i in Ingresso.query.all() if i.lote_origem)

    stats = {
        'lote_ativo_nome': lote_ativo.nome if lote_ativo else 'Nenhum Lote Ativo',
        'lote_ativo_preco': lote_ativo.preco if lote_ativo else 0.0,
        'ingressos_vendidos': vendidos,
        'ingressos_utilizados': utilizados,
        'receita_total': receita_total
    }
    
    return render_template('admin/dashboard.html', stats=stats, lotes=lotes)

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
                    ingresso.data_uso = datetime.utcnow()
                    db.session.commit()
                    flash('ENTRADA LIBERADA! Ingresso marcado como UTILIZADO.', 'success')
            
            resultado = ingresso

    return render_template('admin/validar.html', resultado=resultado, codigo=codigo_buscado)

if __name__ == '__main__':
    app.run(debug=True)
