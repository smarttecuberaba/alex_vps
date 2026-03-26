"""
2W Pneus — Agente Alex v2 (API para Chatwoot)
Roda como servidor Flask que recebe webhooks do Chatwoot.
"""

import os
import json
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from anthropic import Anthropic
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv(override=True)

# ─────────────────────────────────────────
#  Configurações
# ─────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_KEY      = os.getenv("SUPABASE_KEY")
CHATWOOT_URL      = os.getenv("CHATWOOT_URL")
CHATWOOT_TOKEN    = os.getenv("CHATWOOT_TOKEN")
CHATWOOT_ACCOUNT_ID = os.getenv("CHATWOOT_ACCOUNT_ID", "2")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)

# Armazena sessões por conversa do Chatwoot
sessoes = {}     # conversation_id -> sessao
historicos = {}  # conversation_id -> lista de mensagens


# ─────────────────────────────────────────
#  Ferramentas (mesmo do agente.py)
# ─────────────────────────────────────────
TOOLS = [
    {
        "name": "buscar_pneus_por_moto",
        "description": (
            "Busca pneus compatíveis com uma moto. "
            "Use SEMPRE que o cliente mencionar nome, modelo ou apelido da moto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nome_moto": {
                    "type": "string",
                    "description": "Nome, modelo ou apelido da moto informado pelo cliente.",
                }
            },
            "required": ["nome_moto"],
        },
    },
    {
        "name": "buscar_pneus_por_medida",
        "description": (
            "Busca pneus por medida específica. "
            "Use quando o cliente souber a medida. Ex: '90/90-18', '80/100-18'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "medida": {
                    "type": "string",
                    "description": "Medida do pneu. Ex: '90/90-18'",
                }
            },
            "required": ["medida"],
        },
    },
    {
        "name": "buscar_pedidos_cliente",
        "description": "Consulta os pedidos do cliente atual.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "ver_historico_cliente",
        "description": "Mostra o histórico de interações do cliente atual.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "criar_pedido",
        "description": (
            "Cria um pedido ou orçamento formal para o cliente. "
            "Requer: lista de pneus (pneu_id + quantidade + preco_unitario), forma de pagamento e tipo de entrega."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "itens": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pneu_id":        {"type": "string"},
                            "quantidade":     {"type": "integer"},
                            "preco_unitario": {"type": "number"},
                            "posicao":        {"type": "string"},
                        },
                        "required": ["pneu_id", "quantidade", "preco_unitario"],
                    },
                },
                "forma_pagamento": {
                    "type": "string",
                    "enum": ["Dinheiro", "Pix", "Cartão", "Transferência", "Fiado"],
                },
                "tipo_entrega": {
                    "type": "string",
                    "enum": ["Retirada", "Entrega por Rota", "Motoboy", "Correios"],
                },
                "endereco_entrega": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["Orçamento", "Confirmado", "Entregue", "Cancelado"],
                },
                "observacoes": {"type": "string"},
            },
            "required": ["itens", "forma_pagamento", "tipo_entrega", "status"],
        },
    },
    {
        "name": "solicitar_devolucao",
        "description": "Registra solicitação de devolução ou troca de pneu.",
        "input_schema": {
            "type": "object",
            "properties": {
                "descricao": {"type": "string"},
                "motivo": {
                    "type": "string",
                    "enum": ["Defeito", "Tamanho errado", "Arrependimento", "Produto diferente", "Outro"],
                },
                "pedido_codigo": {"type": "string"},
            },
            "required": ["descricao", "motivo"],
        },
    },
    {
        "name": "registrar_interacao",
        "description": "Registra resumo da conversa no histórico do cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo":          {"type": "string", "enum": ["Atendimento", "Orçamento", "Reclamação", "Follow-up", "Pós-venda", "Anotação"]},
                "resumo":        {"type": "string"},
                "sentimento":    {"type": "string", "enum": ["Positivo", "Neutro", "Negativo"]},
                "proxima_acao":  {"type": "string"},
                "resolvido":     {"type": "boolean"},
            },
            "required": ["tipo", "resumo", "sentimento", "resolvido"],
        },
    },
    {
        "name": "atualizar_cliente",
        "description": "Atualiza dados do cliente (nome, bairro, cidade, endereço).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome":       {"type": "string"},
                "bairro":     {"type": "string"},
                "cidade":     {"type": "string", "enum": ["Maricá", "Rio de Janeiro", "Niterói", "Duque de Caxias", "Nova Iguaçu", "São Gonçalo", "Belford Roxo", "Guapimirim", "Itaboraí", "Outra"]},
                "endereco":   {"type": "string"},
                "observacoes":{"type": "string"},
            },
            "required": [],
        },
    },
]


# ─────────────────────────────────────────
#  CRM — Sessão
# ─────────────────────────────────────────
def iniciar_sessao(whatsapp: str) -> dict:
    resp = (
        supabase.table("v_clientes_agente")
        .select("id, nome, status, total_pedidos, valor_total_compras, saldo_credito, observacoes")
        .eq("whatsapp", whatsapp)
        .eq("ativo", True)
        .execute()
    )

    sessao = {"whatsapp": whatsapp, "cliente_id": None, "cliente_nome": None, "cliente_novo": False}

    if resp.data:
        c = resp.data[0]
        sessao["cliente_id"]   = c["id"]
        sessao["cliente_nome"] = c["nome"]
        sessao["cliente_novo"] = False
        return sessao, {
            "cliente_existente": True,
            "nome": c["nome"],
            "total_pedidos": c["total_pedidos"],
            "valor_total_compras": float(c["valor_total_compras"] or 0),
            "saldo_credito": float(c["saldo_credito"] or 0),
            "observacoes": c.get("observacoes", ""),
        }

    novo = (
        supabase.table("clientes")
        .insert({"nome": "Cliente WhatsApp", "whatsapp": whatsapp, "origem": "WhatsApp Orgânico", "status": "Novo"})
        .execute()
    )
    if novo.data:
        sessao["cliente_id"]   = novo.data[0]["id"]
        sessao["cliente_nome"] = "Cliente WhatsApp"
        sessao["cliente_novo"] = True
        return sessao, {"cliente_existente": False}

    return sessao, {"erro": "Falha ao cadastrar cliente."}


# ─────────────────────────────────────────
#  CRM — Ferramentas
# ─────────────────────────────────────────
def atualizar_cliente(sessao, nome=None, bairro=None, cidade=None, endereco=None, observacoes=None):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    payload = {}
    if nome:        payload["nome"] = nome; sessao["cliente_nome"] = nome
    if bairro:      payload["bairro"] = bairro
    if cidade:      payload["cidade"] = cidade
    if endereco:    payload["endereco"] = endereco
    if observacoes: payload["observacoes"] = observacoes
    if not payload:
        return {"mensagem": "Nenhum dado para atualizar."}
    r = supabase.table("clientes").update(payload).eq("id", sessao["cliente_id"]).execute()
    return {"atualizado": bool(r.data), "campos": list(payload.keys())}


def registrar_interacao(sessao, tipo, resumo, sentimento, resolvido, proxima_acao=None):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    payload = {
        "cliente_id": sessao["cliente_id"], "tipo": tipo,
        "canal": "WhatsApp", "resumo": resumo,
        "sentimento": sentimento, "resolvido": resolvido,
    }
    if proxima_acao:
        payload["proxima_acao"] = proxima_acao
    r = supabase.table("interacoes_cliente").insert(payload).execute()
    return {"registrado": bool(r.data)}


def ver_historico_cliente(sessao):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    r = (
        supabase.table("v_interacoes_agente")
        .select("tipo, canal, resumo, sentimento, resolvido, proxima_acao, created_at")
        .eq("cliente_id", sessao["cliente_id"])
        .order("created_at", desc=True).limit(5).execute()
    )
    return {"historico": r.data or [], "total": len(r.data or [])}


def buscar_pedidos_cliente(sessao):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    r = (
        supabase.table("v_pedidos_agente")
        .select("codigo, status, valor_final, forma_pagamento, pago, tipo_entrega, data_pedido, data_entrega, observacoes")
        .eq("cliente_id", sessao["cliente_id"]).eq("ativo", True)
        .order("created_at", desc=True).limit(5).execute()
    )
    return {"pedidos": r.data or [], "total": len(r.data or [])}


def criar_pedido(sessao, itens, forma_pagamento, tipo_entrega, status, endereco_entrega=None, observacoes=None):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    valor_total = sum(i["preco_unitario"] * i["quantidade"] for i in itens)
    pedido_payload = {
        "cliente_id": sessao["cliente_id"], "status": status,
        "valor_total": valor_total, "valor_final": valor_total,
        "forma_pagamento": forma_pagamento, "tipo_entrega": tipo_entrega, "origem": "WhatsApp",
    }
    if endereco_entrega: pedido_payload["endereco_entrega"] = endereco_entrega
    if observacoes:      pedido_payload["observacoes"] = observacoes
    pedido = supabase.table("pedidos").insert(pedido_payload).execute()
    if not pedido.data:
        return {"erro": "Falha ao criar pedido."}
    pedido_id = pedido.data[0]["id"]
    pedido_codigo = pedido.data[0].get("codigo", "")
    itens_payload = []
    for item in itens:
        itens_payload.append({
            "pedido_id": pedido_id, "pneu_id": item["pneu_id"],
            "quantidade": item["quantidade"], "preco_unitario": item["preco_unitario"],
            "subtotal": item["preco_unitario"] * item["quantidade"],
            "posicao": item.get("posicao", ""),
        })
    supabase.table("itens_pedido").insert(itens_payload).execute()
    return {"criado": True, "pedido_codigo": pedido_codigo, "valor_total": valor_total, "status": status}


def solicitar_devolucao(sessao, descricao, motivo, pedido_codigo=None):
    if not sessao.get("cliente_id"):
        return {"erro": "Nenhum cliente na sessão."}
    payload = {"cliente_id": sessao["cliente_id"], "descricao": descricao, "motivo": motivo, "status": "Aberta"}
    if pedido_codigo:
        pedido = (
            supabase.table("v_pedidos_agente").select("id")
            .eq("codigo", pedido_codigo).eq("cliente_id", sessao["cliente_id"]).execute()
        )
        if pedido.data:
            payload["pedido_id"] = pedido.data[0]["id"]
    r = supabase.table("devolucoes").insert(payload).execute()
    if r.data:
        codigo = r.data[0].get("codigo", "")
        return {"registrado": True, "codigo": codigo}
    return {"registrado": False, "erro": "Falha ao registrar devolução."}


def buscar_pneus_por_moto(nome_moto):
    apelidos = (
        supabase.table("motos_apelidos").select("moto_id, apelido, ambiguo")
        .ilike("apelido", f"%{nome_moto}%").execute()
    )
    moto_ids, ambiguo = [], False
    if apelidos.data:
        moto_ids = [r["moto_id"] for r in apelidos.data if r.get("moto_id")]
        ambiguo = any(r.get("ambiguo") for r in apelidos.data)
    else:
        motos = supabase.table("motos").select("id").ilike("nome", f"%{nome_moto}%").eq("ativo", True).execute()
        if motos.data:
            moto_ids = [r["id"] for r in motos.data]
    if not moto_ids:
        return {"encontrado": False, "mensagem": f"Moto '{nome_moto}' não encontrada."}
    motos_info = supabase.table("motos").select("id, nome, marca_moto, cilindrada_cc, ano_inicio, ano_fim").in_("id", moto_ids[:3]).execute()
    resultados = []
    for moto in motos_info.data:
        compat = (
            supabase.table("compatibilidade_pneu_moto").select("posicao, medida, recomendado, pneu_id")
            .eq("moto_id", moto["id"]).eq("ativo", True).execute()
        )
        if not compat.data:
            continue
        pneu_ids = [c["pneu_id"] for c in compat.data if c.get("pneu_id")]
        if not pneu_ids:
            continue
        pneus = (
            supabase.table("v_pneus_agente")
            .select("id, nome, medida, preco_venda, preco_promocional, posicao, tipo, desenho")
            .in_("id", pneu_ids).eq("ativo", True).execute()
        )
        for pneu in pneus.data:
            est = supabase.table("estoque").select("quantidade").eq("pneu_id", pneu["id"]).eq("ativo", True).execute()
            pneu["estoque"] = sum(e["quantidade"] for e in est.data) if est.data else 0
            c_info = next((c for c in compat.data if c["pneu_id"] == pneu["id"]), {})
            pneu["posicao_na_moto"] = c_info.get("posicao", "")
            pneu["recomendado"] = c_info.get("recomendado", False)
        ano_fim = moto.get("ano_fim") or "atual"
        resultados.append({
            "moto": f"{moto['marca_moto']} {moto['nome']} {moto.get('cilindrada_cc','')}cc ({moto.get('ano_inicio','')}–{ano_fim})",
            "pneus": pneus.data,
        })
    if not resultados:
        return {"encontrado": False, "mensagem": "Moto encontrada mas sem pneus cadastrados."}
    return {"encontrado": True, "ambiguo": ambiguo, "resultados": resultados}


def buscar_pneus_por_medida(medida):
    pneus = (
        supabase.table("v_pneus_agente")
        .select("id, nome, medida, preco_venda, preco_promocional, posicao, tipo, desenho")
        .ilike("medida", f"%{medida}%").eq("ativo", True).execute()
    )
    if not pneus.data:
        return {"encontrado": False, "mensagem": f"Nenhum pneu com medida '{medida}'."}
    for pneu in pneus.data:
        est = supabase.table("estoque").select("quantidade").eq("pneu_id", pneu["id"]).eq("ativo", True).execute()
        pneu["estoque"] = sum(e["quantidade"] for e in est.data) if est.data else 0
    return {"encontrado": True, "pneus": pneus.data}


# ─────────────────────────────────────────
#  Despachante
# ─────────────────────────────────────────
def executar_ferramenta(sessao, nome, inputs):
    if nome == "buscar_pneus_por_moto":
        return buscar_pneus_por_moto(**inputs)
    if nome == "buscar_pneus_por_medida":
        return buscar_pneus_por_medida(**inputs)
    if nome == "buscar_pedidos_cliente":
        return buscar_pedidos_cliente(sessao)
    if nome == "ver_historico_cliente":
        return ver_historico_cliente(sessao)
    if nome == "criar_pedido":
        return criar_pedido(sessao, **inputs)
    if nome == "solicitar_devolucao":
        return solicitar_devolucao(sessao, **inputs)
    if nome == "registrar_interacao":
        return registrar_interacao(sessao, **inputs)
    if nome == "atualizar_cliente":
        return atualizar_cliente(sessao, **inputs)
    return {"erro": f"Ferramenta '{nome}' não reconhecida."}


# ─────────────────────────────────────────
#  System prompt
# ─────────────────────────────────────────
def build_system_prompt(sessao):
    contexto_cliente = ""
    if sessao.get("cliente_id"):
        if sessao.get("cliente_novo"):
            contexto_cliente = (
                f"\n\n## Cliente atual\n"
                f"📱 WhatsApp: {sessao['whatsapp']}\n"
                f"🆕 Cliente NOVO — pergunte o nome naturalmente e salve com atualizar_cliente."
            )
        else:
            contexto_cliente = (
                f"\n\n## Cliente atual\n"
                f"📱 WhatsApp: {sessao['whatsapp']}\n"
                f"👤 Nome: {sessao['cliente_nome']}\n"
                f"⚠️ Chame-o pelo nome desde o início."
            )

    return f"""Você é o Alex, atendente virtual da 2W Pneus — loja especializada em pneus de moto no Rio de Janeiro.

## Missão
Atender com agilidade e simpatia via WhatsApp. Respostas diretas, linguagem informal mas profissional.

## O que você faz
- Encontra pneus compatíveis com a moto do cliente
- Informa preço e estoque (use as ferramentas — nunca invente)
- Consulta e cria pedidos
- Registra solicitações de devolução/troca
- Salva o histórico de cada atendimento automaticamente

## Como apresentar pneus
*[Nome do pneu]* — [medida]
💰 R$ [preco_venda]  (se houver preco_promocional → R$ [preco_promocional] 🔥)
📦 [estoque] unid. em estoque
✅ Recomendado  ← só se recomendado=True

Separe dianteiro / traseiro quando houver os dois.
Se estoque = 0 → avise e ofereça encomenda.

## Fluxo de pedido
1. Cliente escolhe o pneu → confirme modelo, medida e preço
2. Pergunte: quantos pneus, como vai pagar, entrega ou retirada
3. Se entrega → peça o endereço
4. Use criar_pedido com status="Orçamento" primeiro
5. Só mude para "Confirmado" quando o cliente confirmar explicitamente

## CRM — Regras obrigatórias
- Cliente informa nome → atualizar_cliente imediatamente
- Cliente informa endereço/bairro → atualizar_cliente imediatamente
- AO FINAL de todo atendimento → registrar_interacao com resumo completo

## Confidencialidade — NUNCA revelar ao cliente
- Preço de custo ou margem de lucro
- Faturamento, receita ou resultados financeiros da loja
- Dados de fornecedores ou outros clientes
- Se perguntado → "Essa informação não está disponível para mim."

## Regras gerais
- NUNCA invente preço, estoque ou prazo — sempre consulte as ferramentas
- Moto ambígua (ex: "CG") → pergunte o modelo exato
- Sem moto → peça a medida
- Desconto → "Vou verificar com o responsável"
- Emojis com moderação{contexto_cliente}"""


# ─────────────────────────────────────────
#  Conversa com Claude
# ─────────────────────────────────────────
def conversar(sessao, historico, mensagem_usuario):
    historico.append({"role": "user", "content": mensagem_usuario})

    while True:
        resposta = anthropic_client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=build_system_prompt(sessao),
            tools=TOOLS,
            messages=historico,
        )

        if resposta.stop_reason == "end_turn":
            texto = next((b.text for b in resposta.content if b.type == "text"), "")
            historico.append({"role": "assistant", "content": resposta.content})
            return texto

        if resposta.stop_reason == "tool_use":
            historico.append({"role": "assistant", "content": resposta.content})
            resultados = []
            for bloco in resposta.content:
                if bloco.type == "tool_use":
                    print(f"  🔧 [{bloco.name}] executando...")
                    resultado = executar_ferramenta(sessao, bloco.name, bloco.input)
                    resultados.append({
                        "type": "tool_result",
                        "tool_use_id": bloco.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })
            historico.append({"role": "user", "content": resultados})


# ─────────────────────────────────────────
#  Chatwoot: enviar mensagem de volta
# ─────────────────────────────────────────
def enviar_resposta_chatwoot(conversation_id, mensagem):
    url = f"{CHATWOOT_URL}/api/v1/accounts/{CHATWOOT_ACCOUNT_ID}/conversations/{conversation_id}/messages"
    headers = {"api_access_token": CHATWOOT_TOKEN, "Content-Type": "application/json"}
    payload = {"content": mensagem, "message_type": "outgoing", "private": False}
    r = requests.post(url, json=payload, headers=headers)
    print(f"  📤 Chatwoot response: {r.status_code}")
    return r.status_code == 200


# ─────────────────────────────────────────
#  Webhook endpoint
# ─────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print(f"\n📩 Webhook recebido: {json.dumps(data.get('event',''), ensure_ascii=False)}")

    # Só processa mensagens incoming (do cliente)
    event = data.get("event")
    if event != "message_created":
        return jsonify({"status": "ignored"}), 200

    message = data.get("content", "")
    message_type = data.get("message_type")
    conversation = data.get("conversation", {})
    conversation_id = conversation.get("id")
    inbox_id = data.get("inbox", {}).get("id")
    sender = data.get("sender", {})

    # Ignora mensagens outgoing (do próprio bot) e private
    if message_type != "incoming":
        return jsonify({"status": "ignored"}), 200

    if not message or not conversation_id:
        return jsonify({"status": "no_content"}), 200

    print(f"  💬 Conversa #{conversation_id} | De: {sender.get('name', '?')} | Msg: {message[:80]}")

    # Extrai whatsapp do contato
    phone = sender.get("phone_number", "") or ""
    # Limpa o número
    whatsapp = phone.replace("+", "").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    if not whatsapp:
        whatsapp = f"chatwoot_{conversation_id}"

    # Inicializa sessão se não existe
    if conversation_id not in sessoes:
        sessao, info = iniciar_sessao(whatsapp)
        sessoes[conversation_id] = sessao
        historicos[conversation_id] = []
        nome = info.get("nome", "novo")
        print(f"  👤 Sessão criada: {nome} ({whatsapp})")

    sessao = sessoes[conversation_id]
    historico = historicos[conversation_id]

    # Limita histórico para não estourar contexto
    if len(historico) > 40:
        historico = historico[-30:]
        historicos[conversation_id] = historico

    # Processa com Claude
    try:
        resposta = conversar(sessao, historico, message)
        print(f"  🤖 Alex: {resposta[:100]}...")
        enviar_resposta_chatwoot(conversation_id, resposta)
    except Exception as e:
        print(f"  ❌ Erro: {e}")
        enviar_resposta_chatwoot(conversation_id, "Desculpe, tive um problema técnico. Pode repetir?")

    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "agent": "Alex v2", "time": datetime.now().isoformat()}), 200


if __name__ == "__main__":
    print("\n🚀 Agente Alex v2 — Webhook Chatwoot")
    print(f"   Chatwoot: {CHATWOOT_URL}")
    print(f"   Account: {CHATWOOT_ACCOUNT_ID}")
    print(f"   Supabase: {SUPABASE_URL}\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
