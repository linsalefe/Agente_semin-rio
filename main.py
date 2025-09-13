import asyncio
import uvicorn
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from loguru import logger
import json
import re
from typing import Any, Dict, Optional

from config.settings import settings
from agents.lead_agent import lead_agent
from database.database import db_manager
from services.whatsapp_service import whatsapp_service

# Configurar logs
logger.add("logs/cenat_agent.log", rotation="1 day", level="INFO")

def _only_digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")

def _extract_phone(payload: Dict[str, Any]) -> Optional[str]:
    """
    Tenta extrair o telefone do JID em vários campos.
    """
    jid = payload.get("jid") or payload.get("key", {}).get("remoteJid") or ""
    jid = str(jid)
    if "@s.whatsapp.net" in jid:
        return _only_digits(jid.replace("@s.whatsapp.net", ""))
    if "@g.us" in jid:
        return _only_digits(jid.replace("@g.us", ""))
    # fallback: às vezes vem no participant
    participant = (
        payload.get("message", {})
               .get("listResponseMessage", {})
               .get("contextInfo", {})
               .get("participant")
        or payload.get("message", {})
                 .get("buttonsResponseMessage", {})
                 .get("contextInfo", {})
                 .get("participant")
    )
    if participant:
        return _only_digits(str(participant).split("@")[0])
    return None

def _extract_user_name(payload: Dict[str, Any]) -> str:
    return payload.get("pushName") or payload.get("senderName") or "Cliente"

def _get_message_type(payload: Dict[str, Any]) -> str:
    # Alguns provedores usam message.type
    return payload.get("messageType") or payload.get("message", {}).get("type") or ""

def _parse_selected_row_id(obj: Dict[str, Any]) -> Optional[str]:
    """
    Procura o selectedRowId/rowId em várias estruturas possíveis.
    """
    # listResponseMessage
    lrm = obj.get("listResponseMessage") or {}
    single = lrm.get("singleSelectReply") or {}
    rid = single.get("selectedRowId") or single.get("rowId")
    if rid:
        return str(rid)

    # interactiveResponseMessage.listResponseMessage
    irm = obj.get("interactiveResponseMessage") or {}
    lrm2 = irm.get("listResponseMessage") or {}
    single2 = lrm2.get("singleSelectReply") or {}
    rid = single2.get("selectedRowId") or single2.get("rowId")
    if rid:
        return str(rid)

    # buttonReplyMessage (algumas libs usam isso)
    br = obj.get("buttonReplyMessage") or {}
    bid = br.get("selectedButtonId") or br.get("id")
    if bid:
        return str(bid)

    # buttonsResponseMessage
    btn = obj.get("buttonsResponseMessage") or {}
    bid = btn.get("selectedButtonId")
    if bid:
        return str(bid)

    # nativeFlowResponseMessage (fluxos nativos do WA)
    nfm = irm.get("nativeFlowResponseMessage") or {}
    params = nfm.get("paramsJson")
    if isinstance(params, str):
        try:
            data = json.loads(params)
            # padrões mais comuns dentro do paramsJson
            rid = data.get("rowId") or data.get("selectedRowId") or data.get("id")
            if rid:
                return str(rid)
        except Exception:
            pass

    return None

def _extract_text(payload: Dict[str, Any]) -> str:
    """
    Extrai texto ou IDs de seleção do payload, cobrindo os principais tipos.
    Retorna string não-vazia quando for possível.
    """
    msg_obj = payload.get("message") or {}
    mtype = _get_message_type(payload)

    # 1) Se for resposta de lista/botão, priorizamos o ID
    rid = _parse_selected_row_id(msg_obj)
    if rid:
        return rid

    # 2) Tipos de texto
    if mtype == "conversation":
        return str(msg_obj.get("conversation") or "")
    if mtype == "extendedTextMessage":
        return str((msg_obj.get("extendedTextMessage") or {}).get("text") or "")

    # 3) Outras formas de chegar texto
    # - imageMessage com caption
    cap = (msg_obj.get("imageMessage") or {}).get("caption")
    if cap:
        return str(cap)

    # - videoMessage com caption
    vcap = (msg_obj.get("videoMessage") or {}).get("caption")
    if vcap:
        return str(vcap)

    # - interactiveMessage com body
    im = msg_obj.get("interactiveMessage") or {}
    body = (im.get("body") or {}).get("text")
    if body:
        return str(body)

    # - texto direto (algumas libs usam message.text)
    if isinstance(msg_obj.get("text"), str):
        return msg_obj["text"]

    # 4) Se nada encontrado, tenta pegar "title" de uma resposta de lista (não é o ideal, mas destrava)
    title = (msg_obj.get("listResponseMessage") or {}).get("title")
    if title:
        return str(title)

    return ""

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia o ciclo de vida da aplicação"""
    logger.info("🚀 Iniciando Agente CENAT...")

    # Verifica conexões
    status = await whatsapp_service.check_instance_status()
    if status.get("connected"):
        logger.info("✅ WhatsApp conectado")
    else:
        logger.warning("⚠️ WhatsApp não conectado")

    # Valida configurações
    try:
        settings.validate_settings()
        logger.info("✅ Configurações validadas")
    except ValueError as e:
        logger.error(f"❌ Erro nas configurações: {e}")

    yield
    logger.info("🛑 Parando Agente CENAT...")

# Criar aplicação FastAPI
app = FastAPI(
    title="CENAT Lead Agent",
    description="Agente de IA para conversão de leads pós-seminário",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/")
async def health_check():
    return {"status": "running", "agent": "CENAT Lead Converter", "version": "1.0.0"}

@app.post("/webhook")
async def webhook_handler(request: Request):
    """Processa webhooks da Mega API (robusto para listas/botões)"""
    try:
        payload = await request.json()
        logger.info(f"📨 Webhook recebido: {json.dumps(payload, indent=2, ensure_ascii=False)}")

        mtype = _get_message_type(payload)
        # ACKs não trazem conteúdo processável
        if mtype == "message.ack":
            phone = _extract_phone(payload) or ""
            logger.warning(f"⚠️ Mensagem sem texto ou telefone: phone='{phone}' text=''")
            return JSONResponse({"status": "ignored", "reason": "ack"})

        # Extrai telefone e nome
        phone = _extract_phone(payload)
        user_name = _extract_user_name(payload)

        # Verifica se é mensagem enviada por nós
        key = payload.get("key", {}) or {}
        from_me = bool(key.get("fromMe"))

        if from_me and settings.IGNORE_FROM_ME:
            logger.info("⏭️ Ignorando mensagem própria")
            return JSONResponse({"status": "ignored", "reason": "from_me"})

        # Extrai texto ou rowId
        message_text = _extract_text(payload)

        if not message_text or not phone:
            logger.warning(f"⚠️ Mensagem sem texto ou telefone: phone='{phone}' text='{message_text}'")
            return JSONResponse({"status": "ignored", "reason": "no_text_or_phone"})

        logger.info(f"📱 Processando: {phone} | {user_name} | {message_text}")

        # Processa com o agente
        response = await lead_agent.handle_message(
            phone=phone,
            message=message_text,
            user_name=user_name
        )

        logger.info(f"🤖 Resposta enviada: {response}")
        return JSONResponse({
            "status": "processed",
            "phone": phone,
            "user_name": user_name,
            "message_received": message_text,
            "response": response
        })

    except Exception as e:
        logger.error(f"❌ Erro no webhook: {e}")
        try:
            raw = await request.body()
            logger.error(f"Payload bruto: {raw}")
        except Exception:
            pass
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/status")
async def get_status():
    whatsapp_status = await whatsapp_service.check_instance_status()
    stats = db_manager.get_conversion_stats()
    return {
        "whatsapp_connected": whatsapp_status.get("connected", False),
        "whatsapp_user": whatsapp_status.get("user"),
        "database_stats": stats,
        "settings": {
            "batch_size": settings.BATCH_SIZE,
            "delay_between_messages": settings.DELAY_BETWEEN_MESSAGES
        }
    }

# === Endpoints de teste com JSON no body (evita 422) ===
@app.post("/test-message")
async def test_message(
    body: Dict[str, Any] = Body(..., example={"phone": "55999999999", "message": "Oi teste!", "user_name": "Alefe"})
):
    """Endpoint para testar mensagens (envie JSON no body)"""
    try:
        phone = _only_digits(body.get("phone", ""))
        message = str(body.get("message", ""))
        user_name = str(body.get("user_name", "Teste"))

        response = await lead_agent.handle_message(phone=phone, message=message, user_name=user_name)
        return {"status": "success", "phone": phone, "message_sent": message, "response": response}
    except Exception as e:
        logger.error(f"❌ Erro no teste: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/start-campaign")
async def start_campaign(
    body: Dict[str, Any] = Body(..., example={"phone": "55999999999", "name": "João", "seminario_nome": "Boas Práticas"})
):
    """Inicia campanha para um lead específico (envie JSON no body)"""
    try:
        phone = _only_digits(body.get("phone", ""))
        name = str(body.get("name", "Cliente"))
        seminario_nome = body.get("seminario_nome")
        success = await lead_agent.start_post_seminar_campaign(phone=phone, name=name, seminario_nome=seminario_nome)
        return {"status": "success" if success else "failed", "phone": phone, "name": name, "campaign_started": success}
    except Exception as e:
        logger.error(f"❌ Erro ao iniciar campanha: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
