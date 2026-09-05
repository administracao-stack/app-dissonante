@app.route('/carrinho/adicionar-multiplo', methods=['POST'])
def adicionar_carrinho_multiplo():
    # 1. Tratar input de form e converter para int de forma segura
    def get_int_field(field_name):
        try:
            val = request.form.get(field_name, 0)
            return int(val) if val else 0
        except (ValueError, TypeError):
            return 0

    qty_teste = get_int_field('qty_teste')
    # CORREÇÃO PRINCIPAL: Alterado de 'qty_promocional' para 'qty_promo'
    qty_promo = get_int_field('qty_promo') 
    qty_lote1_meia = get_int_field('qty_lote1_meia')
    qty_lote1_inteira = get_int_field('qty_lote1_inteira')
    qty_lote2_meia = get_int_field('qty_lote2_meia')
    qty_lote2_inteira = get_int_field('qty_lote2_inteira')

    total_qtd = qty_teste + qty_promo + qty_lote1_meia + qty_lote1_inteira + qty_lote2_meia + qty_lote2_inteira

    if total_qtd <= 0:
        flash('Selecione ao menos um ingresso para continuar.', 'warning')
        return redirect(url_for('evento_marevibes'))

    if 'session_token' not in session:
        session['session_token'] = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    
    session_id = session['session_token']
    carrinho = session.get('carrinho', {})

    # 2. Mapeamento dos lotes no banco de dados
    lotes_db = Lote.query.all()
    mapa_lotes = {
        'teste': next((l for l in lotes_db if l.nome.lower() == 'teste'), None),
        'promo': next((l for l in lotes_db if 'promocional' in l.nome.lower()), None),
        'lote1_meia': next((l for l in lotes_db if '1º lote - meia' in l.nome.lower()), None),
        'lote1_inteira': next((l for l in lotes_db if '1º lote - inteira' in l.nome.lower()), None),
        'lote2_meia': next((l for l in lotes_db if '2º lote - meia' in l.nome.lower()), None),
        'lote2_inteira': next((l for l in lotes_db if '2º lote - inteira' in l.nome.lower()), None)
    }

    quantidades = [
        ('teste', qty_teste),
        ('promo', qty_promo),
        ('lote1_meia', qty_lote1_meia),
        ('lote1_inteira', qty_lote1_inteira),
        ('lote2_meia', qty_lote2_meia),
        ('lote2_inteira', qty_lote2_inteira)
    ]

    try:
        for chave, qtd in quantidades:
            if qtd > 0:
                lote = mapa_lotes.get(chave)
                if not lote or not lote.ativo:
                    continue

                # Lock pessimista para concorrencia no banco
                db.session.query(Lote).filter_by(id=lote.id).with_for_update().first()

                disponiveis = obter_estoque_disponivel(lote.id, session_id_atual=session_id)
                str_lote_id = str(lote.id)

                if qtd > disponiveis:
                    db.session.rollback()
                    flash(f'Desculpe, restam apenas {disponiveis} ingressos disponíveis para o {lote.nome}.', 'danger')
                    return redirect(url_for('evento_marevibes'))

                expiracao = datetime.now(timezone.utc) + timedelta(minutes=MINUTOS_RESERVA)
                reserva = ReservaCarrinho.query.filter_by(session_id=session_id, lote_id=lote.id).first()

                if reserva:
                    reserva.quantidade += qtd
                    reserva.data_expiracao = expiracao
                else:
                    reserva = ReservaCarrinho(
                        session_id=session_id,
                        lote_id=lote.id,
                        quantidade=qtd,
                        data_expiracao=expiracao
                    )
                    db.session.add(reserva)

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

        db.session.commit()
        session['carrinho'] = carrinho
        session.modified = True
        flash('Ingressos reservados e adicionados ao carrinho por 15 minutos!', 'success')

    except Exception as e:
        db.session.rollback()
        flash('Erro ao reservar os ingressos. Tente novamente.', 'danger')

    return redirect(url_for('ver_carrinho'))
