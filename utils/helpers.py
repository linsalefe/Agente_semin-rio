import re
from pathlib import Path
from typing import List, Dict
from loguru import logger

class SimpleRAG:
    """Sistema RAG simples para base de conhecimento CENAT"""
    
    def __init__(self, knowledge_file: str = "data/knowledge_base.txt"):
        self.knowledge_file = Path(knowledge_file)
        self.knowledge_base = ""
        self.load_knowledge()
    
    def load_knowledge(self):
        """Carrega base de conhecimento"""
        try:
            if self.knowledge_file.exists():
                self.knowledge_base = self.knowledge_file.read_text(encoding="utf-8")
                logger.info(f"✅ Base de conhecimento carregada: {len(self.knowledge_base)} caracteres")
            else:
                logger.warning(f"❌ Arquivo não encontrado: {self.knowledge_file}")
                self.knowledge_base = "Base de conhecimento não disponível."
        except Exception as e:
            logger.error(f"❌ Erro ao carregar base de conhecimento: {e}")
            self.knowledge_base = "Erro ao carregar base de conhecimento."
    
    def search_context(self, query: str) -> str:
        """Busca contexto relevante baseado na query"""
        query_lower = query.lower()
        
        # Termos para buscar diferentes seções
        if any(term in query_lower for term in ['seminário', 'seminario', 'direitos humanos']):
            return self._extract_section("## SEMINÁRIOS CENAT")
        
        elif any(term in query_lower for term in ['pós', 'pos', 'graduação', 'especialização']):
            return self._extract_section("## PÓS-GRADUAÇÕES CENAT")
        
        elif any(term in query_lower for term in ['cenat', 'empresa', 'instituição']):
            return self._extract_section("## INFORMAÇÕES GERAIS")
        
        else:
            # Retorna tudo se não conseguir identificar
            return self.knowledge_base
    
    def _extract_section(self, section_header: str) -> str:
        """Extrai uma seção específica da base de conhecimento"""
        try:
            lines = self.knowledge_base.split('\n')
            section_lines = []
            in_section = False
            
            for line in lines:
                if line.strip() == section_header:
                    in_section = True
                    section_lines.append(line)
                elif line.startswith('## ') and in_section:
                    # Nova seção começou
                    break
                elif in_section:
                    section_lines.append(line)
            
            return '\n'.join(section_lines) if section_lines else self.knowledge_base
            
        except Exception as e:
            logger.error(f"❌ Erro ao extrair seção: {e}")
            return self.knowledge_base
    
    def get_current_seminario(self) -> Dict:
        """Extrai informações do seminário atual"""
        try:
            seminario_section = self._extract_section("## SEMINÁRIOS CENAT")
            
            # Regex para extrair informações básicas
            data_match = re.search(r'Data: (.+)', seminario_section)
            investimento_match = re.search(r'Investimento: (.+)', seminario_section)
            
            return {
                'nome': 'Direitos Humanos e Saúde Mental das Populações Vulnerabilizadas',
                'data': data_match.group(1) if data_match else 'A definir',
                'investimento': investimento_match.group(1) if investimento_match else 'A consultar'
            }
            
        except Exception as e:
            logger.error(f"❌ Erro ao extrair seminário atual: {e}")
            return {
                'nome': 'Seminário CENAT',
                'data': 'A definir',
                'investimento': 'A consultar'
            }

def clean_phone(phone: str) -> str:
    """Limpa e formata número de telefone"""
    # Remove tudo exceto números
    digits = re.sub(r'\D', '', phone)
    
    # Garante que comece com 55 (código do Brasil)
    if len(digits) == 11 and digits.startswith('55'):
        return digits
    elif len(digits) == 11:
        return '55' + digits
    elif len(digits) == 13 and digits.startswith('55'):
        return digits
    else:
        return digits

def format_phone_whatsapp(phone: str) -> str:
    """Formata telefone para WhatsApp"""
    clean = clean_phone(phone)
    return f"{clean}@s.whatsapp.net"

# Instância global
rag = SimpleRAG()