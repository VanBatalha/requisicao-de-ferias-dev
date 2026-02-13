from __future__ import annotations

from .base import bp
from ..core import *  # noqa: F401,F403

@bp.route("/api/solicitar-ferias", methods=["POST"])
def api_solicitar_ferias():
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado."}), 401

    gestor_email = safe_lower(user.get("email") or "")
    if not gestor_email:
        return jsonify({"ok": False, "message": "Usuário inválido."}), 400

    role = get_user_role(gestor_email)
    is_dp_or_admin = role in ("DP", "admin")

    # Regra:
    # - Gestores solicitam para sua equipe
    # - DP/Admin podem solicitar para qualquer colaborador ativo
    if not (is_dp_or_admin or is_gestor(gestor_email)):
        return jsonify({"ok": False, "message": "Apenas gestores (ou DP/Admin) podem solicitar férias."}), 403

    colaborador_email = safe_lower(request.form.get("colaborador_email") or request.form.get("colaborador") or "")
    tipo_solicitacao = (request.form.get("tipo_solicitacao") or request.form.get("tipo_solicitacao_out") or "").strip()

    data_inicio_str = request.form.get("data_inicio")
    data_fim_str = request.form.get("data_fim")
    observacoes = (request.form.get("observacoes") or "").strip()

    # "Tipo de Férias" (nome de UI) continua usando o campo saldo_tipo para compatibilidade
    saldo_tipo_req = (request.form.get("saldo_tipo") or request.form.get("tipo_ferias") or "REGULAR").strip().upper()
    if saldo_tipo_req not in ("REGULAR", "PREMIUM"):
        saldo_tipo_req = "REGULAR"

    # Licença Certariana (PREMIUM) agora é solicitada em parcelas fixas:
    # - 2 períodos de 15 dias OU 3 períodos de 10 dias
    cert_formato = (request.form.get("certariana_formato") or "").strip()
    cert_inicio_1 = (request.form.get("cert_inicio_1") or "").strip()
    cert_inicio_2 = (request.form.get("cert_inicio_2") or "").strip()
    cert_inicio_3 = (request.form.get("cert_inicio_3") or "").strip()

    certariana_segmentos = []  # [{"ini": date, "fim": date, "dias": int, "ini_str": str, "fim_str": str}]
    cert_total_dias = 0

    if not colaborador_email:
        return jsonify({"ok": False, "message": "Selecione o colaborador."}), 400

    if not tipo_solicitacao:
        # Licença Certariana (PREMIUM) é sempre Gozo — deixa mais robusto caso o JS do front não rode
        if saldo_tipo_req == "PREMIUM":
            tipo_solicitacao = "Gozo"
        else:
            return jsonify({"ok": False, "message": "Selecione o tipo de solicitação (Venda ou Gozo)."}), 400

    tipo_norm = tipo_solicitacao.strip().lower()
    if tipo_norm in ("usufruir", "usufruto", "gozar", "gozo"):
        tipo_solicitacao_out = "Gozo"
    elif tipo_norm in ("venda", "vender"):
        tipo_solicitacao_out = "Venda"
    else:
        # aceita exatamente o que veio, mas valida mínimo
        if tipo_norm not in ("venda", "gozo"):
            return jsonify({"ok": False, "message": "Tipo inválido. Use Venda ou Gozo."}), 400
        tipo_solicitacao_out = tipo_solicitacao.title()

    # valida se colaborador está no escopo
    if is_dp_or_admin:
        permitidos = set(listar_emails_colaboradores(only_ativos=True))
        if colaborador_email not in permitidos:
            return jsonify({"ok": False, "message": "Colaborador não encontrado (ou não está Ativo no cadastro)."}), 400
    else:
        permitidos = set(get_subordinados(gestor_email))  # gestor não solicita para si nesta tela
        if colaborador_email not in permitidos:
            return jsonify({"ok": False, "message": "Colaborador não pertence à sua equipe (ou não está vinculado ao seu gestor)."}), 403

    # =============================
    # Datas / Segmentos
    # =============================
    if saldo_tipo_req == "PREMIUM":
        # Licença Certariana: 1 período por solicitação (respeitando regras de fracionamento)
        tipo_solicitacao_out = "Gozo"

        if not data_inicio_str or not data_fim_str:
            return jsonify({"ok": False, "message": "Para Licença Certariana, informe Data início e Data fim do período (mínimo 10 dias)."}), 400

        try:
            dt_inicio = dt.datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
            dt_fim = dt.datetime.strptime(data_fim_str, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "message": "Datas inválidas."}), 400

        if dt_fim < dt_inicio:
            return jsonify({"ok": False, "message": "Data fim não pode ser menor que data início."}), 400

        ok_periodo, msg = periodo_permitido(dt_inicio, dt_fim, requester_email=gestor_email)
        if not ok_periodo:
            return jsonify({"ok": False, "message": msg}), 400

        # Mantido por compatibilidade: não usamos mais os 'cert_inicio_*' (formato fixo),
        # mas a variável segue existindo para não quebrar outros trechos.
        certariana_segmentos = []
        cert_total_dias = 0

    else:
        # Fluxo padrão (Regular)
        if not data_inicio_str or not data_fim_str:
            return jsonify({"ok": False, "message": "Datas obrigatórias."}), 400

        dt_inicio = dt.datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
        dt_fim = dt.datetime.strptime(data_fim_str, "%Y-%m-%d").date()

        if dt_fim < dt_inicio:
            return jsonify({"ok": False, "message": "Data fim não pode ser menor que data início."}), 400

        ok_periodo, msg = periodo_permitido(dt_inicio, dt_fim, requester_email=gestor_email)
        if not ok_periodo:
            return jsonify({"ok": False, "message": msg}), 400
    if not is_dp_or_admin:

        # Regra adicional (cadastro 3609445264215940):
        # - Se REGIME DE CONTRATAÇÃO == CLT e for a 1ª solicitação, só permitir a partir de 1 ano e 9 meses (21 meses) de empresa.
        try:
            regime = (_colaborador_regime(colaborador_email) or "").strip().upper()
            adm = _colaborador_admissao(colaborador_email)
            if regime == "CLT" and adm:
                resumo_tmp = get_resumo_ferias(colaborador_email)
                if resumo_tmp.get("total_solicitacoes", 0) <= 0:
                    liberado_em = _add_months(adm, 21)
                    if dt_inicio < liberado_em:
                        return jsonify({
                            "ok": False,
                            "message": f"Para regime CLT, a 1ª solicitação só é permitida a partir de {liberado_em.strftime('%d/%m/%Y')} (1 ano e 9 meses de empresa)."
                        }), 400
        except Exception as _e:
            # se não conseguir validar, não bloqueia (mantém fluxo)
            pass


    try:
        resumo = get_resumo_ferias(colaborador_email)
        dias_novos = cert_total_dias if (saldo_tipo_req == "PREMIUM" and certariana_segmentos) else (dt_fim - dt_inicio).days + 1

        # Validação adicional: regras de fracionamento da Licença Certariana (considera períodos já lançados)
        if saldo_tipo_req == "PREMIUM" and not certariana_segmentos:
            try:
                direito_total = int(resumo.get("premium", {}).get("direito", 0) or 0)

                adm_c = _colaborador_admissao(colaborador_email)
                _, win_start, win_end = _janela_licenca_certariana(adm_c, hoje=dt_inicio) if adm_c else (0, None, None)

                # Considera APROVADA + RESERVA (pendente/em análise) para travar fracionamentos inválidos
                include_statuses = set(STATUS_APROVADA) | set(STATUS_RESERVA)
                existentes = _listar_segmentos_premium(colaborador_email, win_start, win_end, include_statuses=include_statuses)

                ok_frac, msg_frac = _validar_fracionamento_certariana(
                    direito_total=direito_total,
                    dt_inicio=dt_inicio,
                    dt_fim=dt_fim,
                    dias_novos=int(dias_novos),
                    segmentos_existentes=existentes,
                )
                if not ok_frac:
                    return jsonify({"ok": False, "message": msg_frac}), 400
            except Exception as _e:
                # Se falhar, não bloqueia (mantém compatibilidade), mas loga
                print(f"[CERTARIANA] Falha ao validar fracionamento: {_e}")

        reg_saldo = int(resumo["regular"]["saldo"])
        prem_saldo = int(resumo["premium"]["saldo"])
        
        saldo_tipo_final = saldo_tipo_req
        
        if saldo_tipo_req == "REGULAR":
            if dias_novos <= reg_saldo:
                saldo_tipo_final = "REGULAR"
            else:
                return jsonify({
                    "ok": False,
                    "message": f"Saldo insuficiente. Regular: {reg_saldo} dias. Para usar Licença Certariana, selecione 'Licença Certariana' em Tipo de Férias e informe 2×15 ou 3×10."
                }), 400
        else:
            if dias_novos > prem_saldo:
                return jsonify({"ok": False, "message": f"Saldo da Licença Certariana insuficiente: {prem_saldo} dias."}), 400
            saldo_tipo_final = "PREMIUM"

    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao validar saldo de férias: {e}"}), 500

    # saldo base usado para o cálculo final
    saldo_base = reg_saldo if saldo_tipo_final == "REGULAR" else prem_saldo

    # Validação extra: Licença Certariana não é cumulativa e só pode ser usada dentro da janela vigente (2 anos)
    # (Regra aplicada para USER; DP/Admin podem registrar exceções.)
    if saldo_tipo_final == "PREMIUM" and not is_dp_or_admin:
        try:
            adm_c = _colaborador_admissao(colaborador_email)

            # Se não houver janela (ex.: ainda não completou 5 anos) e também não houver saldo por ajuste, bloqueia.
            # Se houver saldo (por ajuste/manual do DP), permite mesmo sem admissão.
            if not adm_c and prem_saldo <= 0:
                return jsonify({"ok": False, "message": "Licença Certariana ainda não está disponível para este colaborador."}), 400

            # Valida por segmento (quando Certariana), ou pelo período único (casos antigos)
            segs = []
            if certariana_segmentos:
                segs = [{"ini": s["ini"], "fim": s["fim"]} for s in certariana_segmentos]
            else:
                segs = [{"ini": dt_inicio, "fim": dt_fim}]

            for seg in segs:
                dias_base, win_start, win_end = _janela_licenca_certariana(adm_c, hoje=seg["ini"]) if adm_c else (0, None, None)

                # Sem janela: só permite se existir saldo por ajuste
                if not (win_start and win_end):
                    if prem_saldo <= 0:
                        return jsonify({"ok": False, "message": "Licença Certariana ainda não está disponível para este colaborador."}), 400
                    continue

                # Janela é [win_start, win_end) (fim exclusivo)
                if not (win_start <= seg["ini"] < win_end and win_start <= seg["fim"] < win_end):
                    fim_incl = (win_end - dt.timedelta(days=1)).strftime("%d/%m/%Y")
                    ini = win_start.strftime("%d/%m/%Y")
                    return jsonify({
                        "ok": False,
                        "message": f"Licença Certariana só pode ser utilizada entre {ini} e {fim_incl} (não cumulativa e válida por 2 anos após a conquista)."
                    }), 400
        except Exception:
            # Se não conseguir validar por algum motivo, não bloqueia o fluxo (mantém compatibilidade)
            pass

    # grava marcador no texto (sem duplicar)
    marker = f"Saldo: {saldo_tipo_final}"
    if marker.lower() not in (observacoes or "").lower():
        observacoes = (observacoes + ("\n" if observacoes else "") + marker).strip()

    def add_cell_unique(cells_by_id: dict, col_id, value):
        """Adiciona célula garantindo unicidade por column_id (evita erro 1037)."""
        try:
            cid = int(col_id) if col_id is not None else 0
        except Exception:
            cid = 0
        if cid > 0:
            # sobrescreve se já existir (último valor ganha)
            cells_by_id[cid] = value


    try:
        client = get_smartsheet_client()
        if not client:
            return jsonify({"ok": False, "message": "Smartsheet client não inicializado (sem token)."}), 500

        sheet_sol = get_sheet_solicitacoes(client)
        
        # Colunas robustas
        col_colab = col_id_by_name(sheet_sol, "COLABORADOR")
        col_gestor = col_id_by_name(sheet_sol, "GESTOR SOLICITANTE")
        col_solic = col_id_by_name(sheet_sol, "SOLICITAÇÃO", "SOLICITACAO")
        col_inicio = col_id_by_name(sheet_sol, "DATA INICIO", "DATA INÍCIO", "DATA INICIAL")
        col_fim = col_id_by_name(sheet_sol, "DATA FIM", "DATA FINAL")
        col_dias = col_id_by_name(sheet_sol, "DIAS")
        col_status = col_id_by_name(sheet_sol, "STATUS")
        col_obs = col_id_by_name(sheet_sol, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVAÇÃO", "OBSERVACAO")
        col_saldo_tipo = col_id_by_name(sheet_sol, "SALDO TIPO", "SALDO_TIPO", "TIPO DE FERIAS", "TIPO DE FÉRIAS", "TIPO FERIAS")

        rows_to_add = []

        if saldo_tipo_final == "PREMIUM" and certariana_segmentos:
            total_parcelas = len(certariana_segmentos)
            for idx, seg in enumerate(certariana_segmentos, start=1):
                
                r = smartsheet.models.Row()
                r.to_top = True
                cells = {}

                add_cell_unique(cells, col_colab, colaborador_email)
                add_cell_unique(cells, col_gestor, gestor_email)
                add_cell_unique(cells, col_saldo_tipo, saldo_tipo_final)
                add_cell_unique(cells, col_solic, "Gozo")
                add_cell_unique(cells, col_inicio, seg["ini_str"])
                add_cell_unique(cells, col_fim, seg["fim_str"])
                add_cell_unique(cells, col_dias, seg["dias"])
                add_cell_unique(cells, col_status, "PENDENTE")

                obs_row = observacoes
                obs_row = (obs_row + ("\n" if obs_row else "") + f"Saldo tipo: {saldo_tipo_final}").strip()
                obs_row = (obs_row + ("\n" if obs_row else "") + f"Licença Certariana: parcela {idx}/{total_parcelas}").strip()
                add_cell_unique(cells, col_obs, obs_row)

                r.cells = build_cells(cells)

                ensure_primary_cell(sheet_sol, r, colaborador_email)
                rows_to_add.append(r)
        else:
            
            new_row = smartsheet.models.Row()
            new_row.to_top = True
            cells = {}

            # Grava o email do colaborador na coluna COLABORADOR
            add_cell_unique(cells, col_colab, colaborador_email)
            add_cell_unique(cells, col_gestor, gestor_email)
            add_cell_unique(cells, col_saldo_tipo, saldo_tipo_final)
            add_cell_unique(cells, col_solic, tipo_solicitacao_out)

            add_cell_unique(cells, col_inicio, data_inicio_str)
            add_cell_unique(cells, col_fim, data_fim_str)
            add_cell_unique(cells, col_dias, dias_novos)
            add_cell_unique(cells, col_status, "PENDENTE")

            # Observações (opcional) -> coluna OBSERVAÇÕES
            add_cell_unique(cells, col_obs, observacoes)

            new_row.cells = build_cells(cells)

            ensure_primary_cell(sheet_sol, new_row, colaborador_email)
            rows_to_add.append(new_row)

        # Debug/diagnóstico: ajuda a identificar ID de planilha, colunas e retorno do Smartsheet
        print("[SOLICITAR-FERIAS] Gravando no sheet:", ID_FOLHA_SOLICITACOES)
        print("[SOLICITAR-FERIAS] Col IDs:", {
            "colab": col_colab, "gestor": col_gestor,
            "fim": col_fim, "dias": col_dias, "status": col_status, "obs": col_obs, "saldo_tipo": col_saldo_tipo
        })

        resp = client.Sheets.add_rows(ID_FOLHA_SOLICITACOES, rows_to_add)
        inserted_ids = []
        try:
            inserted_ids = [getattr(r, "id", None) for r in (resp.result or [])]
            print("[SOLICITAR-FERIAS] inserted row ids:", inserted_ids)
        except Exception:
            pass
        invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
    except Exception as e:
        return jsonify({"ok": False, "message": f"Erro ao salvar solicitação: {e}"}), 500

    saldo_atualizado = saldo_base - dias_novos

    if saldo_tipo_final == "PREMIUM" and certariana_segmentos:
        return jsonify({
            "ok": True,
            "sheet_id": ID_FOLHA_SOLICITACOES,
            "inserted_ids": inserted_ids,
            "message": f"Solicitação registrada (Licença Certariana) em {len(certariana_segmentos)} parcela(s), total {dias_novos} dia(s). Saldo restante: {saldo_atualizado}.",
            "saldo_atualizado": saldo_atualizado
        })

    return jsonify({
        "ok": True,
        "message": f"Solicitação registrada ({tipo_solicitacao_out}) com {dias_novos} dia(s). Saldo restante: {saldo_atualizado}.",
        "saldo_atualizado": saldo_atualizado
    })

@bp.route("/api/editar-solicitacao", methods=["POST"])
def api_editar_solicitacao():
    user = session.get("user")
    if not user:
        return jsonify({"ok": False, "message": "Não autenticado."}), 401
    
    email = user.get("email")
    row_id = request.form.get("row_id")
    data_inicio_str = request.form.get("data_inicio")
    data_fim_str = request.form.get("data_fim")
    
    if not row_id or not data_inicio_str or not data_fim_str:
        return jsonify({"ok": False, "message": "Parâmetros obrigatórios."}), 400
    
    dt_inicio_novo = dt.datetime.strptime(data_inicio_str, "%Y-%m-%d").date()
    dt_fim_novo = dt.datetime.strptime(data_fim_str, "%Y-%m-%d").date()
    
    if dt_fim_novo < dt_inicio_novo:
        return jsonify({"ok": False, "message": "Data fim não pode ser menor que data início."}), 400
    
    ok_periodo, msg = periodo_permitido(dt_inicio_novo, dt_fim_novo, requester_email=email)
    if not ok_periodo:
        return jsonify({"ok": False, "message": msg}), 400
    
    try:
        client = get_smartsheet_client()
        sheet_sol = get_sheet_solicitacoes(client)
        cols_sol = get_col_map(sheet_sol)

        row_id_int = int(row_id)
        row_antiga = next((r for r in sheet_sol.rows if r.id == row_id_int), None)

        if not row_antiga:
            return jsonify({"ok": False, "message": "Solicitação não encontrada."}), 404

        status_atual = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("STATUS", -1)),
            ""
        )

        if safe_lower(status_atual) != "pendente":
            return jsonify({"ok": False, "message": "Só é possível editar solicitações com status Pendente."})

        # Identifica colaborador e tipo de saldo da linha
        col_colab_id = col_id_by_name(sheet_sol, "COLABORADOR")
        col_obs_id = col_id_by_name(sheet_sol, "OBSERVAÇÕES", "OBSERVACOES", "OBSERVAÇÃO", "OBSERVACAO")
        col_tipo_id = col_id_by_name(sheet_sol, "SALDO TIPO", "SALDO_TIPO", "TIPO DE FERIAS", "TIPO DE FÉRIAS", "TIPO FERIAS")

        colab_email_row = next((c.value for c in row_antiga.cells if c.column_id == (col_colab_id or -1)), "") or ""
        colab_email_row = safe_lower(str(colab_email_row))

        obs_row = next((c.value for c in row_antiga.cells if c.column_id == (col_obs_id or -1)), "") or ""
        explicit_tipo_row = next((c.value for c in row_antiga.cells if c.column_id == (col_tipo_id or -1)), "") or ""

        saldo_tipo_row = _infer_saldo_tipo(obs_row, explicit_tipo_row)

        # Saldos por tipo (REGULAR vs PREMIUM)
        resumo = get_resumo_ferias(colab_email_row)
        if saldo_tipo_row == "PREMIUM":
            dias_direito = int(resumo["premium"]["direito"])
            dias_usados = int(resumo["premium"]["usados"])
            dias_reservados = int(resumo["premium"]["reservados"])
        else:
            dias_direito = int(resumo["regular"]["direito"])
            dias_usados = int(resumo["regular"]["usados"])
            dias_reservados = int(resumo["regular"]["reservados"])

        dias_antigos = next(
            (c.value for c in row_antiga.cells if c.column_id == cols_sol.get("DIAS", -1)),
            0
        ) or 0
        try:
            dias_antigos = int(float(dias_antigos or 0))
        except Exception:
            dias_antigos = 0

        saldo_atual = dias_direito - dias_usados - dias_reservados
        saldo_ajustado = saldo_atual + dias_antigos

        dias_novos = (dt_fim_novo - dt_inicio_novo).days + 1

        if saldo_tipo_row == "PREMIUM":
            # Regras de fracionamento/overlap (considerando outras linhas)
            adm_c = _colaborador_admissao(colab_email_row)
            _, win_start, win_end = _janela_licenca_certariana(adm_c, hoje=dt_inicio_novo) if adm_c else (0, None, None)
            include_statuses = set(STATUS_APROVADA) | set(STATUS_RESERVA)
            existentes = _listar_segmentos_premium(
                colab_email_row, win_start, win_end,
                exclude_row_id=row_id_int,
                include_statuses=include_statuses
            )
            ok_frac, msg_frac = _validar_fracionamento_certariana(
                direito_total=dias_direito,
                dt_inicio=dt_inicio_novo,
                dt_fim=dt_fim_novo,
                dias_novos=int(dias_novos),
                segmentos_existentes=existentes,
            )
            if not ok_frac:
                return jsonify({"ok": False, "message": msg_frac})

        if dias_novos > saldo_ajustado:
            return jsonify({
                "ok": False,
                "message": f"Saldo insuficiente após ajuste. Você tem {saldo_ajustado} dia(s) disponível(is)."
            })

        row_update = smartsheet.models.Row()
        row_update.id = row_id_int
        row_update.cells = build_cells({
            cols_sol.get("DATA INICIO", -1): data_inicio_str,
            cols_sol.get("DATA FIM", -1): data_fim_str,
            cols_sol.get("DIAS", -1): dias_novos,
        })
        
        client.Sheets.update_rows(ID_FOLHA_SOLICITACOES, [row_update])
        invalidate_sheet_cache(ID_FOLHA_SOLICITACOES)
        
        saldo_final = saldo_ajustado - dias_novos
    except Exception as e:
        print(f"ERRO em api_editar_solicitacao: {e}")
        return jsonify({"ok": False, "message": f"Erro ao editar solicitação: {e}"}), 500
    
    return jsonify({
        "ok": True,
        "message": f"Solicitação atualizada para {dias_novos} dia(s). Saldo restante: {saldo_final}.",
        "saldo_atualizado": saldo_final
    })
def build_cells(cells_by_id: dict):
        """Converte {column_id: value} em lista de Cell() do SDK (evita linha em branco)."""
        out = []
        for cid, val in cells_by_id.items():
            try:
                cid_int = int(cid)
            except Exception:
                continue
            if cid_int <= 0:
                continue
            c = smartsheet.models.Cell()
            c.column_id = cid_int
            c.value = val
            out.append(c)
        return out


