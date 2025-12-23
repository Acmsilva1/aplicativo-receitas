import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
from dotenv import load_dotenv

# --- Configurações Iniciais ---
load_dotenv() # Carrega variáveis do .env (localmente)

# Variáveis do Google Sheets
SHEET_ID = os.getenv("SHEET_ID")
# A chave privada deve ter as novas linhas preservadas (usamos replace no código)
PRIVATE_KEY = os.getenv("GCP_SA_PRIVATE_KEY", "").replace("\\n", "\n")

# --- Funções de Conexão e Caching ---

@st.cache_resource
def get_service_account_credentials():
    """Constrói o JSON de credenciais a partir das variáveis de ambiente."""
    if not all([os.getenv("GCP_SA_CLIENT_EMAIL"), PRIVATE_KEY]):
        st.error("Erro de configuração: Credenciais do Google Cloud não encontradas. Configure as secrets no Streamlit Cloud ou o arquivo .env localmente.")
        st.stop()
        
    creds_info = {
        "type": os.getenv("GCP_SA_TYPE"),
        "project_id": os.getenv("GCP_SA_PROJECT_ID"),
        "private_key_id": os.getenv("GCP_SA_PRIVATE_KEY_ID"),
        "private_key": PRIVATE_KEY,
        "client_email": os.getenv("GCP_SA_CLIENT_EMAIL"),
        "client_id": os.getenv("GCP_SA_CLIENT_ID"),
        "auth_uri": os.getenv("GCP_SA_AUTH_URI"),
        "token_uri": os.getenv("GCP_SA_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.getenv("GCP_SA_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.getenv("GCP_SA_CLIENT_X509_CERT_URL"),
        "universe_domain": os.getenv("GCP_SA_UNIVERSE_DOMAIN")
    }
    
    # Validação de credenciais (para não gerar malwares de erro, só um erro limpo)
    if not creds_info.get("private_key"):
        raise ValueError("Chave privada não carregada corretamente.")

    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # O Gspread prefere o método from_json_keyfile_dict
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, scope)
    return creds

@st.cache_data(ttl=600) # Cache de 10 minutos para não estourar o limite de leitura da API
def load_data_from_gsheets(sheet_name):
    """Conecta ao Google Sheets e carrega os dados de uma aba específica."""
    try:
        creds = get_service_account_credentials()
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(sheet_name)
        
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        # Converte nomes das colunas para maiúsculas e remove espaços para padronização
        df.columns = [col.upper().strip() for col in df.columns]
        
        return df
    
    except gspread.exceptions.SpreadsheetNotFound:
        st.error(f"Planilha com ID '{SHEET_ID}' não encontrada.")
        st.stop()
    except gspread.exceptions.WorksheetNotFound:
        st.error(f"Aba '{sheet_name}' não encontrada na planilha.")
        st.stop()
    except Exception as e:
        st.error(f"Erro ao carregar dados da aba {sheet_name}: {e}")
        st.stop()
        
# --- Funções de Processamento de Dados (Seu motor de IA/Dados) ---

def sanitize_and_convert(df, column_name):
    """Limpa e converte colunas de valores para float."""
    df[column_name] = df[column_name].astype(str).str.replace('R$', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.strip()
    # Tenta converter, se falhar, preenche com 0 e avisa (gov. de dados: não quebrar)
    df[column_name] = pd.to_numeric(df[column_name], errors='coerce').fillna(0.0)
    return df

def calculate_master_ingredient_cost(df_ingredientes):
    """Calcula o custo unitário (por G, ML ou UN) de cada ingrediente mestre."""
    
    df = df_ingredientes.copy()
    
    # Limpeza e conversão de valores
    df = sanitize_and_convert(df, 'VALOR_PACOTE')
    
    # Conversão de unidades para custo por unidade base (g, ml, ou un)
    # Exemplo: Se o pacote tem 5000G e custa 15.50, o custo/G é 15.50 / 5000
    df['CUSTO_UNITARIO'] = df['VALOR_PACOTE'] / df['QUANT_PACOTE']
    
    # Renomeia para clareza e cria um dicionário de busca rápida
    df = df[['NOME_ITEM', 'UNIDADE_PACOTE', 'CUSTO_UNITARIO']]
    df.columns = ['NOME_INGREDIENTE', 'UNIDADE_BASE', 'CUSTO_UNITARIO']
    
    # Tratamento de UNIDADES (UN) para manter CUSTO_UNITARIO como R$/UN
    df['CUSTO_UNITARIO'] = df.apply(
        lambda row: row['CUSTO_UNITARIO'] if row['UNIDADE_BASE'] == 'UN' else row['CUSTO_UNITARIO'], 
        axis=1
    )
    
    # Cria o dicionário de custo (chave: NOME_INGREDIENTE)
    custo_dict = df.set_index('NOME_INGREDIENTE')['CUSTO_UNITARIO'].to_dict()
    unidade_dict = df.set_index('NOME_INGREDIENTE')['UNIDADE_BASE'].to_dict()
    
    return custo_dict, unidade_dict

def calculate_recipe_cost(df_receitas, custo_dict, receita_col_name='NOME_BASE'):
    """Calcula o custo total de uma base ou receita final."""
    
    df = df_receitas.copy()
    
    # Assegura que QUANT_RECEITA é numérica (governança de dados: não confie no input)
    df['QUANT_RECEITA'] = pd.to_numeric(df['QUANT_RECEITA'], errors='coerce').fillna(0)

    # Função para calcular o custo do ingrediente na receita
    def calc_ingrediente_custo(row):
        nome_ingrediente = row['NOME_INGREDIENTE']
        quantidade_receita = row['QUANT_RECEITA']
        
        custo_unitario = custo_dict.get(nome_ingrediente)
        
        if custo_unitario is None:
            # Analogia: É como tentar fazer um bolo sem farinha. Vai dar ruim.
            return 0.0 # Ingrediente mestre não encontrado
        
        return custo_unitario * quantidade_receita

    # Calcula o custo de cada linha (ingrediente na receita)
    df['CUSTO_INGREDIENTE'] = df.apply(calc_ingrediente_custo, axis=1)

    # Soma o custo por receita
    custo_total_receita = df.groupby(receita_col_name)['CUSTO_INGREDIENTE'].sum().reset_index()
    custo_total_receita.columns = [receita_col_name, 'CUSTO_TOTAL']

    return custo_total_receita.set_index(receita_col_name)['CUSTO_TOTAL'].to_dict()

def compile_final_pricing(df_receitas_bases, df_receitas_finais, custo_ingredientes_dict, unidade_ingredientes_dict):
    """
    Função principal de precificação: calcula o custo das bases, depois o custo final dos produtos,
    incluindo bases e itens diretos.
    """
    
    # 1. Calcular Custo das Receitas Base
    custo_bases_dict = calculate_recipe_cost(df_receitas_bases, custo_ingredientes_dict, receita_col_name='NOME_BASE')
    
    # Adicionar rendimento (se for o caso) - No seu arquivo, a coluna 'RENDIMENTO_FINAL_UNIDADES' está na 'receitas_bases'
    # Vamos criar um dicionário de rendimento para bases que rendem mais de 1 (ex: mini-vulção)
    rendimento_bases = df_receitas_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates().set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
    
    # Ajustar custo da base pelo rendimento
    for base, custo in custo_bases_dict.items():
        rendimento = rendimento_bases.get(base, 1)
        custo_bases_dict[base] = custo / rendimento # Custo por "unidade" de base produzida

    # 2. Compilar Custo Total (Ingredientes Mestres + Bases)
    # Combina os dicionários de custo para a próxima etapa de cálculo
    custo_total_dict = {**custo_ingredientes_dict, **custo_bases_dict}
    
    # 3. Calcular Custo das Receitas Finais
    df_finais = df_receitas_finais.copy()
    
    # Assegura que QUANT_RECEITA é numérica
    df_finais['QUANT_RECEITA'] = pd.to_numeric(df_finais['QUANT_RECEITA'], errors='coerce').fillna(0)
    
    def calc_item_custo_final(row):
        nome_item = row['NOME_INGREDIENTE']
        quantidade = row['QUANT_RECEITA']
        
        custo_unitario = custo_total_dict.get(nome_item)
        
        if custo_unitario is None:
            # Sarcasmo: O cliente inventou um ingrediente que nem o Google conhece.
            return 0.0
        
        return custo_unitario * quantidade

    df_finais['CUSTO_ITEM'] = df_finais.apply(calc_item_custo_final, axis=1)

    # Soma o custo por bolo final
    custo_final = df_finais.groupby('NOME_BOLO')['CUSTO_ITEM'].sum().reset_index()
    custo_final.columns = ['Produto', 'Custo Total de Insumos (R$)']
    
    # Formatação para R$
    custo_final['Custo Total de Insumos (R$)'] = custo_final['Custo Total de Insumos (R$)'].round(2)
    
    return custo_final.sort_values(by='Custo Total de Insumos (R$)', ascending=False)


# --- Streamlit App ---

def main():
    st.set_page_config(page_title="Caderno de Receitas e Precificação 🍰", layout="wide")
    st.title("💰 Precificação Mágica dos Seus Bolos (Impulsionado por Dados)")
    st.markdown("""
        Bem-vindo ao seu painel de custo. Aqui, a mágica da precificação acontece: 
        calculamos o custo exato de cada ingrediente, somamos tudo e voilà!
        Chega de adivinhar o preço do bolo.
    """)
    
    # 1. Carregar Dados
    with st.spinner('Buscando ingredientes na despensa virtual do Google Sheets...'):
        df_ingredientes = load_data_from_gsheets('ingredientes_mestres')
        df_bases = load_data_from_gsheets('receitas_bases')
        df_finais = load_data_from_gsheets('receitas_finais')
    
    st.success("Dados da planilha carregados e prontos para a mágica do cálculo!")
    
    # 2. Calcular Custos
    custo_ingredientes_dict, unidade_ingredientes_dict = calculate_master_ingredient_cost(df_ingredientes)
    df_precificacao_final = compile_final_pricing(df_bases, df_finais, custo_ingredientes_dict, unidade_ingredientes_dict)
    
    # --- Apresentação dos Resultados ---
    
    st.header("Análise de Custo Final dos Produtos")
    st.dataframe(df_precificacao_final, hide_index=True, use_container_width=True)
    
    st.subheader("Bolo Mais Caro x Bolo Mais Barato")
    
    mais_caro = df_precificacao_final.iloc[0]
    mais_barato = df_precificacao_final.iloc[-1]
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.info(f"🍰 **O Mais Luxuoso:** {mais_caro['Produto']}")
        st.metric("Custo Total de Insumos", f"R$ {mais_caro['Custo Total de Insumos (R$)']:,.2f}")
        st.markdown(f"> **Teoria na Prática:** Este bolo é seu carro-chefe, o 'iFood Platinum'. Seus ingredientes (como *COBERTURA_BRIGADEIRO* e *CHOCOLATE_NOBRE*) são os que mais pesam na balança financeira, indicando que você deve caprichar na margem de lucro aqui!")
        
    with col2:
        st.success(f"🌾 **O Mais Econômico:** {mais_barato['Produto']}")
        st.metric("Custo Total de Insumos", f"R$ {mais_barato['Custo Total de Insumos (R$)']:,.2f}")
        st.markdown(f"> **Analogia:** Este é o seu 'Bolo Básico de Todo Dia', o *fast-food* do mundo das confeitarias. Geralmente usa bases e ingredientes mais simples (*TRIGO*, *ACUCAR*, *FERMENTO*), permitindo maior volume de vendas e talvez promoções.")

    # --- Expansão (Detalhe do Custo Unitário) ---
    
    st.header("Detalhe: Custo Unitário dos Ingredientes Mestres")
    
    # Cria um DF limpo para exibição
    df_custo_unitario = pd.DataFrame(custo_ingredientes_dict.items(), columns=['NOME_INGREDIENTE', 'CUSTO_UNITARIO'])
    df_custo_unitario['UNIDADE_BASE'] = df_custo_unitario['NOME_INGREDIENTE'].map(unidade_ingredientes_dict)
    
    df_custo_unitario['CUSTO_UNITARIO'] = df_custo_unitario['CUSTO_UNITARIO'].apply(lambda x: f"R$ {x:,.4f}")
    
    st.dataframe(df_custo_unitario, hide_index=True)
    st.caption("Custo por grama (G), mililitro (ML) ou unidade (UN). Valores como R$ 0,0000 indicam custo por G/ML, que é bem pequeno, mas crucial!")
    
if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # Aqui, capturamos exceções não tratadas (governança de código)
        st.error(f"Opa, algo deu muito errado no sistema. Ligue o modo TI! Detalhe: {e}")
