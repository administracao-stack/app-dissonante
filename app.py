import os
import re
import random
import string
from datetime import datetime, timedelta
from functools import wraps
import mercadopago
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configurações do App e Banco de Dados (PostgreSQL / Render)
# --------------------------------------------------------------------------
app.secret_key = os.environ.get('SECRET_KEY', 'chave_secreta_para_desenvolvimento')

# Duração estendida da sessão para evitar deslogar no checkout
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# URL do Banco de Dados
database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///dev.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --------------------------------------------------------------------------
# Configuração do Mercado Pago
# --------------------------------------------------------------------------
MERCADOPAGO_TOKEN = os.getenv('MP_ACCESS_TOKEN')
sdk = mercadopago.SDK(MERCADOPAGO_TOKEN)

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
    data_criacao = db.Column(db.DateTime, default=datetime.utcnow)

    ingressos = db.relationship('Ingresso', backref='comprador', lazy=True)


class Lote(db.Model):
    __tablename__ = 'lotes'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(50), nullable=False)
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


def inicializar_banco():
    """Cria o esquema do banco de dados e semente inicial de forma segura."""
    with app.app_context():
        db.create_all()
        
        # Garante a existência do Lote Promocional (R$ 160,00) - Limitado a 15 unidades
        lote_promo = Lote.query.filter(Lote.nome.ilike('%promocional%')).first()
        if not lote_promo:
            lote_promo = Lote(nome='Lote Promocional', preco=160.00, quantidade_total=15, ativo=True)
            db.session.add(lote_promo)
        else:
            lote_promo.preco = 160.00
            lote_promo.quantidade_total = 15

        # Garante a existência do 1º Lote (R$ 230,00)
        lote_1 = Lote.query.filter(Lote.nome.ilike('%1º lote%')).first()
        if not lote_1:
            lote_1 = Lote(nome='1º Lote', preco=230.00, quantidade_total=150, ativo=False)
            db.session.add(lote_1)
        else:
            lote_1.preco = 230.00

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

# --------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------

@app.route('/checkout', methods=['GET', 'POST'])
@cliente_required
def checkout():
    lote_id_req = request.args.get('lote_id', type=int)
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
        quantidade = int(request.form.get('quantidade', 1))
        metodo_pagamento = request.form.get('metodo_pagamento', 'pix')
        
        if 'promocional' in lote_ativo.nome.lower() and metodo_pagamento != 'pix':
            flash('O Lote Promocional aceita apenas pagamento via Pix.', 'warning')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

        ingressos_vendidos_lote = Ingresso.query.filter_by(lote_id=lote_ativo.id).count()
        disponiveis_lote = lote_ativo.quantidade_total - ingressos_vendidos_lote

        if quantidade > disponiveis_lote:
            flash(f'Restam apenas {disponiveis_lote} ingresso(s) no {lote_ativo.nome}.', 'danger')
            return redirect(url_for('checkout', lote_id=lote_ativo.id))

        # ⚠️ MODIFICAÇÃO DE TESTE: Sobrescrevendo o preço unitário para R$ 1,00
        preco_unitario = 1.00
        total = quantidade * preco_unitario

        nome_completo = (usuario_atual.nome or 'Cliente').strip().split(' ', 1)
        first_name = nome_completo[0]
        last_name = nome_completo[1] if len(nome_completo) > 1 else "Silva"

        cpf_usuario = re.sub(r'\D', '', usuario_atual.cpf or '')
        ddd_tel, num_tel = extrair_ddd_e_numero(usuario_atual.telefone)

        # Trata estrutura do Payer exigida pela API do Mercado Pago
        payer_payload = {
            "email": usuario_atual.email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": {
                "area_code": ddd_tel,
                "number": num_tel
            }
        }

        # Adiciona a identificação apenas se houver CPF cadastrado
        if len(cpf_usuario) == 11:
            payer_payload["identification"] = {
                "type": "CPF",
                "number": cpf_usuario
            }

        # --- PAGAMENTO VIA PIX ---
        if metodo_pagamento == 'pix':
            payment_data = {
                "transaction_amount": float(total),
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes [TESTE]",
                "payment_method_id": "pix",
                "external_reference": f"{usuario_atual.id}|{lote_ativo.id}|{quantidade}",
                "payer": payer_payload
            }

            try:
                payment_response = sdk.payment().create(payment_data)
                payment = payment_response.get("response", {})
                status_code = payment_response.get("status")

                print(f"--- [DEBUG LOG PIX] Status: {status_code} ---")
                print("Response Payload:", payment)

                if status_code in [200, 201] and payment.get("status") in ["pending", "approved"]:
                    pix_info = payment.get("point_of_interaction", {}).get("transaction_data", {})
                    session['compra_atual'] = {
                        'metodo_pagamento': 'pix',
                        'payment_id': payment.get("id"),
                        'lote_id': lote_ativo.id,
                        'total': float(total),
                        'quantidade': quantidade,
                        'qr_code': pix_info.get("qr_code"),
                        'qr_code_base64': pix_info.get("qr_code_base64")
                    }
                    return redirect(url_for('pagamento'))
                else:
                    # Captura mensagens detalhadas da API do Mercado Pago
                    cause = payment.get("cause", [])
                    detalhe = cause[0].get("description") if cause else payment.get("message", "Erro desconhecido")
                    flash(f'Erro MP ({status_code}): {detalhe}', 'danger')
                    return redirect(url_for('checkout', lote_id=lote_ativo.id))

            except Exception as e:
                print("Exceção fatal ao criar PIX:", str(e))
                flash('Falha de conexão com o Mercado Pago. Tente novamente.', 'danger')
                return redirect(url_for('checkout', lote_id=lote_ativo.id))

        # --- PAGAMENTO VIA CARTÃO DE CRÉDITO ---
        elif metodo_pagamento == 'credit_card':
            token = request.form.get('token')
            installments = int(request.form.get('installments', 1))
            payment_method_id = request.form.get('payment_method_id')

            if installments > 2:
                installments = 2

            payment_data = {
                "transaction_amount": float(total),
                "token": token,
                "description": f"{quantidade}x Ingresso ({lote_ativo.nome}) - MaréVibes [TESTE]",
                "installments": installments,
                "payment_method_id": payment_method_id,
                "external_reference": f"{usuario_atual.id}|{lote_ativo.id}|{quantidade}",
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
                            usuario_id=usuario_atual.id,
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
                        'total': float(total),
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
                        'total': float(total),
                        'quantidade': quantidade
                    }
                    flash('Pagamento em análise pelo seu banco. Acompanhe o status nesta página.', 'info')
                    return redirect(url_for('pagamento'))
                else:
                    flash('Cartão recusado ou dados incorretos. Tente novamente.', 'danger')
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

# Webhook Mercado Pago
@app.route('/webhook/mercadopago', methods=['POST'])
def webhook_mercadopago():
    data = request.get_json() or {}
    if data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        payment_info = sdk.payment().get(payment_id).get("response", {})
        
        if payment_info.get("status") == "approved":
            if not Ingresso.query.filter_by(pagamento_id=str(payment_id)).first():
                ext_ref = payment_info.get("external_reference", "")
                
                if ext_ref and "|" in ext_ref:
                    user_id, lote_id, qtd = map(int, ext_ref.split("|"))
                    usuario = Usuario.query.get(user_id)
                    lote = Lote.query.get(lote_id)
                else:
                    payer_email = payment_info.get("payer", {}).get("email")
                    usuario = Usuario.query.filter_by(email=payer_email).first()
                    lote = Lote.query.filter_by(ativo=True).first()
                    qtd = 1

                if usuario and lote:
                    for _ in range(qtd):
                        novo_ingresso = Ingresso(
                            codigo_qr=gerar_codigo_ingresso(),
                            usuario_id=usuario.id,
                            lote_id=lote.id,
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
    
    receita_total = db.session.query(func.sum(Lote.preco))\
        .join(Ingresso, Ingresso.lote_id == Lote.id)\
        .scalar() or 0.0

    stats = {
        'lote_ativo_nome': lote_ativo.nome if lote_ativo else 'Nenhum Lote Ativo',
        'lote_ativo_preco': lote_ativo.preco if lote_ativo else 0.0,
        'ingressos_vendidos': vendidos,
        'ingressos_utilizados': utilizados,
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
                    ingresso.data_uso = datetime.utcnow()
                    db.session.commit()
                    flash('ENTRADA LIBERADA! Ingresso marcado como UTILIZADO.', 'success')
            
            resultado = ingresso

    return render_template('admin/validar.html', resultado=resultado, codigo=codigo_buscado)

# --------------------------------------------------------------------------
# Inicialização
# --------------------------------------------------------------------------

inicializar_banco()

if __name__ == '__main__':
    app.run(debug=True)
