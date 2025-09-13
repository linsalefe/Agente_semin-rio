import httpx
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from loguru import logger
from config.settings import settings

class WhatsAppService:
    """Serviço para integração com MEGA API WhatsApp - CENAT Seminário DH"""
    
    def __init__(self):
        self.base_url = settings.MEGA_API_BASE_URL
        self.token = settings.MEGA_API_TOKEN
        self.instance_id = settings.MEGA_INSTANCE_ID
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        
        # Rate limiting
        self.last_sent = {}
        self.min_interval = 5  # segundos entre mensagens
        
        # Informações do seminário
        self.seminario_info = {
            "nome": "Seminário Online: Direitos Humanos e Saúde Mental das Populações Vulnerabilizadas",
            "data": "24 e 25 de Setembro de 2025",
            "horario": "19h às 22h",
            "plataforma": "DoityPlay",
            "valor": "R$ 19,97",
            "carga_horaria": "6 horas",
            "contato_vendas": "(47) 99242-8886",
            "email": "atendimento@cenatcursos.com.br"
        }
    
    def _digits_only(self, phone: str) -> str:
        """Remove tudo exceto dígitos"""
        return "".join(ch for ch in phone if ch.isdigit())
    
    def _to_whatsapp_format(self, phone: str) -> str:
        """Converte telefone para formato WhatsApp"""
        digits = self._digits_only(phone)
        return f"{digits}@s.whatsapp.net" if digits else phone
    
    async def check_instance_status(self) -> Dict[str, Any]:
        """Verifica status da instância"""
        url = f"{self.base_url}/rest/instance/{self.instance_id}"
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                
                is_connected = bool(data.get("instance", {}).get("user"))
                logger.info(f"📱 Status da instância: {'Conectada' if is_connected else 'Desconectada'}")
                
                return {
                    "connected": is_connected,
                    "instance_data": data.get("instance", {}),
                    "user": data.get("instance", {}).get("user")
                }
                
        except Exception as e:
            logger.error(f"❌ Erro ao verificar status: {e}")
            return {"connected": False, "error": str(e)}
    
    def _check_rate_limit(self, phone: str) -> bool:
        """Verifica se pode enviar mensagem (rate limiting)"""
        now = datetime.now()
        last_sent = self.last_sent.get(phone)
        
        if last_sent:
            time_diff = (now - last_sent).total_seconds()
            if time_diff < self.min_interval:
                logger.warning(f"⏰ Rate limit: aguardar {self.min_interval - time_diff:.0f}s para {phone}")
                return False
        
        return True
    
    async def send_text_message(self, phone: str, message: str) -> bool:
        """Envia mensagem de texto"""
        if not self._check_rate_limit(phone):
            return False
        
        url = f"{self.base_url}/rest/sendMessage/{self.instance_id}/text"
        to = self._to_whatsapp_format(phone)
        
        payload = {
            "messageData": {
                "to": to,
                "text": message
            }
        }
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                
                self.last_sent[phone] = datetime.now()
                logger.info(f"📱 Mensagem enviada para {phone}: {message[:50]}...")
                return True
                
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ Erro HTTP {e.response.status_code} ao enviar para {phone}: {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"❌ Erro ao enviar para {phone}: {e}")
            return False
    
    async def send_initial_contact(self, phone: str, name: str) -> bool:
        """Envia mensagem inicial de contato"""
        message = f"""Oi {name}! 👋 

Sou da equipe do CENAT e vi que você se interessou pelo nosso **Seminário Online: Direitos Humanos e Saúde Mental das Populações Vulnerabilizadas**.

🗓️ **Quando:** 24 e 25 de Setembro de 2025
⏰ **Horário:** 19h às 22h  
💻 **Onde:** Online pela DoityPlay
💰 **Investimento:** Apenas R$ 19,97
🎓 **Certificação:** 6h com validade nacional

Para começar nossa conversa, me conta: você atua em qual área?

🔹 Saúde Mental  
🔹 Educação  
🔹 Assistência Social  
🔹 Outra área

Responde aí que vou te explicar melhor sobre o seminário! 😊"""
        
        return await self.send_text_message(phone, message)
    
    async def send_qualification_options(self, phone: str, name: str) -> bool:
        """Envia lista de qualificação"""
        sections = [
            {
                "title": "🎯 Em qual área você atua?",
                "rows": [
                    {
                        "title": "🧠 Saúde Mental",
                        "description": "Psicólogo, psiquiatra, terapeuta ocupacional...",
                        "rowId": "area_saude_mental"
                    },
                    {
                        "title": "🏥 Saúde Geral",
                        "description": "Enfermeiro, médico, técnico...",
                        "rowId": "area_saude_geral"
                    },
                    {
                        "title": "📚 Educação",
                        "description": "Professor, pedagogo, coordenador...",
                        "rowId": "area_educacao"
                    },
                    {
                        "title": "🤝 Assistência Social",
                        "description": "Assistente social, educador social...",
                        "rowId": "area_assistencia"
                    },
                    {
                        "title": "📋 Outra área",
                        "description": "Gestão, direito, outras...",
                        "rowId": "area_outra"
                    }
                ]
            }
        ]
        
        return await self.send_list_message(
            phone=phone,
            button_text="Selecionar Área",
            text=f"Perfeito {name}! 😊\n\nPara te ajudar melhor com informações sobre o seminário:",
            title="Vamos nos conhecer melhor!",
            description="Selecione sua área de atuação",
            sections=sections
        )
    
    async def send_interest_qualification(self, phone: str, name: str) -> bool:
        """Envia qualificação sobre interesse específico"""
        sections = [
            {
                "title": "🔍 O que mais te interessa no seminário?",
                "rows": [
                    {
                        "title": "👥 Populações Vulnerabilizadas",
                        "description": "Trabalhar com grupos em situação de vulnerabilidade",
                        "rowId": "interesse_populacoes"
                    },
                    {
                        "title": "⚖️ Direitos Humanos",
                        "description": "Compreender a interface DH x Saúde Mental",
                        "rowId": "interesse_direitos"
                    },
                    {
                        "title": "🌈 Diversidade e Inclusão",
                        "description": "Questões de gênero, raça e sexualidade",
                        "rowId": "interesse_diversidade"
                    },
                    {
                        "title": "🎓 Desenvolvimento Profissional",
                        "description": "Qualificar minha prática profissional",
                        "rowId": "interesse_desenvolvimento"
                    }
                ]
            }
        ]
        
        return await self.send_list_message(
            phone=phone,
            button_text="Escolher Interesse",
            text=f"Ótimo {name}! 👏\n\nAgora me conta:",
            title="Qual seu maior interesse?",
            description="Isso vai me ajudar a personalizar as informações",
            sections=sections
        )
    
    async def send_seminario_details(self, phone: str, name: str, interest: str = None) -> bool:
        """Envia detalhes completos do seminário"""
        message = f"""Perfeita escolha {name}! 🎯

Nosso **Seminário de Direitos Humanos e Saúde Mental** é EXATAMENTE para profissionais como você.

📋 **O QUE VOCÊ VAI APRENDER:**

🔸 **24/09** - Direitos Humanos x Saúde Mental (Luciana Alleluia)
🔸 **24/09** - Raça, etnia e interseccionalidades (Rachel Gouveia)  
🔸 **25/09** - Formação para populações vulnerabilizadas (Claudio Mann)
🔸 **25/09** - Gêneros e Sexualidades (Marcos Signorelli)

🎯 **IDEAL PARA:**
✅ Profissionais da saúde, educação e assistência
✅ Quem quer atendimento mais inclusivo e humanizado  
✅ Desenvolver práticas que respeitam a diversidade

💰 **INVESTIMENTO:** Apenas R$ 19,97
🎓 **CERTIFICAÇÃO:** 6h com validade nacional
📚 **BÔNUS:** E-book com material das palestras

Você tem interesse em garantir sua vaga?"""
        
        return await self.send_text_message(phone, message)
    
    async def send_objection_handling(self, phone: str, objection_type: str) -> bool:
        """Trata objeções comuns"""
        messages = {
            "preco": """Entendo sua preocupação! 💰

Olha só: R$ 19,97 para 6h de certificação + 4 palestrantes especialistas + material exclusivo = menos de R$ 3,50 por hora de capacitação!

É um investimento simbólico que cabe no orçamento de qualquer profissional. Pode até parcelar no cartão!

O conhecimento que você vai adquirir vale muito mais que isso. Topa garantir sua vaga?""",
            
            "tempo": """Eu entendo a correria do dia a dia! ⏰

Mas olha que legal: são apenas 2 noites (24 e 25/09), das 19h às 22h. 

É online, então você assiste de casa, sem trânsito. E fica gravado caso precise assistir depois!

São só 6h que podem transformar sua prática profissional. Vale muito a pena, não acha?""",
            
            "relevancia": """Totalmente relevante para você! 🎯

Hoje em dia TODOS os profissionais precisam saber lidar com diversidade e direitos humanos. É exigência do mercado!

Seja na saúde, educação ou assistência, você vai atender pessoas LGBTI+, de diferentes raças, em situação de vulnerabilidade...

Esse conhecimento vai te diferenciar e te preparar melhor. Faz sentido?"""
        }
        
        message = messages.get(objection_type, messages["relevancia"])
        return await self.send_text_message(phone, message)
    
    async def send_urgency_and_close(self, phone: str, name: str) -> bool:
        """Cria urgência e fecha a venda"""
        message = f"""⚠️ **ATENÇÃO {name}!**

As inscrições são **LIMITADAS** e podem encerrar antes do prazo!

Já temos mais de 59 mil pessoas impactadas pelos eventos do CENAT. Esse seminário promete lotar rapidinho! 🔥

🎯 **PARA GARANTIR SUA VAGA:**

1️⃣ Acesse: cenatsaudemental.com/seminario-online-dh-e-populacoes-vulnerabilizadas

2️⃣ Ou fale direto comigo no WhatsApp: {self.seminario_info["contato_vendas"]}

3️⃣ **PIX, boleto ou cartão em até 12x**

Não deixe essa oportunidade passar! Sua carreira merece esse investimento.

Vai garantir sua vaga agora? 😊"""
        
        return await self.send_text_message(phone, message)
    
    async def send_contact_info(self, phone: str) -> bool:
        """Envia informações de contato"""
        return await self.send_contact_message(
            phone=phone,
            contact_name="CENAT - Equipe Comercial",
            contact_phone="5547992428886",
            organization="CENAT Saúde Mental"
        )
    
    async def send_list_message(self, phone: str, button_text: str, text: str, 
                               title: str, description: str, sections: List[Dict]) -> bool:
        """Envia mensagem com lista de opções"""
        if not self._check_rate_limit(phone):
            return False
        
        url = f"{self.base_url}/rest/sendMessage/{self.instance_id}/listMessage"
        to = self._to_whatsapp_format(phone)
        
        payload = {
            "messageData": {
                "to": to,
                "buttonText": button_text,
                "text": text,
                "title": title,
                "description": description,
                "sections": sections,
                "listType": 0
            }
        }
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                
                self.last_sent[phone] = datetime.now()
                logger.info(f"📋 Lista enviada para {phone}: {title}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Erro ao enviar lista para {phone}: {e}")
            return False
    
    async def send_contact_message(self, phone: str, contact_name: str, 
                                 contact_phone: str, organization: str = None) -> bool:
        """Envia cartão de contato"""
        if not self._check_rate_limit(phone):
            return False
        
        url = f"{self.base_url}/rest/sendMessage/{self.instance_id}/contactMessage"
        to = self._to_whatsapp_format(phone)
        
        payload = {
            "messageData": {
                "to": to,
                "vcard": {
                    "fullName": contact_name,
                    "displayName": contact_name,
                    "organization": organization or contact_name,
                    "phoneNumber": contact_phone
                }
            }
        }
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                
                self.last_sent[phone] = datetime.now()
                logger.info(f"👤 Contato enviado para {phone}: {contact_name}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Erro ao enviar contato para {phone}: {e}")
            return False

# Instância global
whatsapp_service = WhatsAppService()