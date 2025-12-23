import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
# from dotenv import load_dotenv # Não é mais necessário para o Cloud, mas mantemos a dependência no requirements.txt

# --- Configurações Iniciais ---
# load_dotenv() # Comentado para ambiente Streamlit Cloud

# Variáveis do Google Sheets (Vem das Secrets)
SHEET_ID = os.getenv("SHEET_ID")
PRIVATE_KEY = os.getenv("GCP_SA_PRIVATE_KEY", "").replace("\\n", "\n")
CLIENT_EMAIL = os.getenv("GCP_SA_CLIENT_EMAIL")

# --- Funções de Conexão e Caching ---

@st.cache_resource
def get_service_account_credentials():
    """Constrói o JSON de credenciais a partir das variáveis de ambiente."""
    if not all([CLIENT_EMAIL, PRIVATE_KEY]):
        st.error("Erro de configuração: Credenciais do Google Cloud não encontradas. Configure as secrets no Streamlit Cloud.")
        st.stop()
        
    creds_info = {
        "type": os.getenv("GCP_SA_TYPE"),
        "project_id": os.getenv("GCP_SA_PROJECT_ID"),
        "private_key_id": os.getenv("GCP_SA_PRIVATE_KEY_ID"),
        "private_key": PRIVATE_KEY,
        "client_email": CLIENT_EMAIL,
        "client_id": os.getenv("GCP_SA_CLIENT_ID"),
        "auth_uri": os.getenv("GCP_SA_AUTH_URI"),
        "token_uri": os.getenv("GCP_SA_TOKEN_URI"),
        "auth_provider_x509_cert_url": os.getenv("GCP_SA_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": os.getenv("GCP_SA_CLIENT_X509_CERT_URL"),
        "universe_domain": os.getenv("GCP_SA_UNIVERSE_DOMAIN")
    }
    
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, scope)
    return creds

@st.cache_data(ttl=600) # Cache de 10 minutos
def load_data_from_gsheets(sheet_name):
    """Conecta ao Google Sheets e carrega os dados de uma aba específica."""
    try:
        creds = get_service_account_credentials()
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(sheet_name)
        
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        df.columns = [col.upper().strip() for col in df.columns]
        
        return df
    
    except Exception as e:
        st.error(f"Erro ao carregar dados da aba {sheet_name}: {e}")
        st.stop()

# --- Funções de Processamento de Dados (Seu motor de IA/Dados) ---

def sanitize_and_convert(df, column_name):
    """Limpa e converte colunas de valores para float."""
    # Aprimoramento: tratar possíveis colunas inexistentes ou vazias
    if column_name not in df.columns:
        return df 
        
    df[column_name] = df[column_name].astype(str).str.replace('R$', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.strip()
    df[column_name] = pd.to_numeric(df[column_name], errors='coerce').fillna(0.0)
    return df

def calculate_master_ingredient_cost(df_ingredientes):
    """Calcula o custo unitário (por G, ML ou UN) de cada ingrediente mestre."""
    
    df = df_ingredientes.copy()
    df = sanitize_and_convert(df, 'VALOR_PACOTE')
    
    # Previne divisão por zero (Governança de Dados)
    df['QUANT_PACOTE'] = df['QUANT_PACOTE'].replace(0, 1)
    
    df['CUSTO_UNITARIO'] = df['VALOR_PACOTE'] / df['QUANT_PACOTE']
    
    df = df[['NOME_ITEM', 'UNIDADE_PACOTE', 'CUSTO_UNITARIO']]
    df.columns = ['NOME_INGREDIENTE', 'UNIDADE_BASE', 'CUSTO_UNITARIO']
    
    custo_dict = df.set_index('NOME_INGREDIENTE')['CUSTO_UNITARIO'].to_dict()
    unidade_dict = df.set_index('NOME_INGREDIENTE')['UNIDADE_BASE'].to_dict()
    
    return custo_dict, unidade_dict

def calculate_recipe_cost(df_receitas, custo_dict, receita_col_name='NOME_BASE'):
    """Calcula o custo total de uma base ou receita final, e retorna o detalhe."""
    
    df = df_receitas.copy()
    
    # Assegura que QUANT_RECEITA é numérica
    df['QUANT_RECEITA'] = pd.to_numeric(df['QUANT_RECEITA'], errors='coerce').fillna(0)

    # Função para calcular o custo do ingrediente na receita
    def calc_ingrediente_custo(row):
        nome_ingrediente = row['NOME_INGREDIENTE']
        quantidade_receita = row['QUANT_RECEITA']
        custo_unitario = custo_dict.get(nome_ingrediente)
        
        # Se não houver custo unitário, o item é desconhecido, custo = 0
        if custo_unitario is None:
            return 0.0
        
        return custo_unitario * quantidade_receita

    # Calcula o custo de cada linha (ingrediente na receita)
    df['CUSTO_UNITARIO'] = df['NOME_INGREDIENTE'].apply(lambda x: custo_dict.get(x, 0.0))
    df['CUSTO_ITEM'] = df.apply(calc_ingrediente_custo, axis=1)

    # Soma o custo por receita
    custo_total_receita_dict = df.groupby(receita_col_name)['CUSTO_ITEM'].sum().to_dict()
    
    return custo_total_receita_dict, df # Retorna o dicionário de custo e o DF de detalhe

@st.cache_data(ttl=600)
def get_all_calculated_data():
    """Carrega todos os dados, calcula os custos intermediários e finais."""
    
    # 1. Carregar Dados
    df_ingredientes = load_data_from_gsheets('ingredientes_mestres')
    df_bases = load_data_from_gsheets('receitas_bases')
    df_finais = load_data_from_gsheets('receitas_finais')
    
    # 2. Calcular Custo Mestre (Ingredientes Primos)
    custo_ingredientes_dict, unidade_ingredientes_dict = calculate_master_ingredient_cost(df_ingredientes)
    
    # 3. Calcular Custo das Receitas Base
    custo_bases_dict, df_bases_detalhe = calculate_recipe_cost(df_bases, custo_ingredientes_dict, receita_col_name='NOME_BASE')
    
    # 4. Ajustar custo da base pelo rendimento
    df_rendimento = df_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates()
    df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1)
    rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
    
    custo_bases_ajustado_dict = {}
    for base, custo in custo_bases_dict.items():
        rendimento = rendimento_bases.get(base, 1)
        custo_bases_ajustado_dict[base] = custo / rendimento # Custo por "unidade" de base produzida

    # 5. Compilar Custo Total (Ingredientes Mestres + Bases Ajustadas)
    custo_total_dict = {**custo_ingredientes_dict, **custo_bases_ajustado_dict}
    
    # 6. Calcular Custo das Receitas Finais
    custo_finais_dict, df_finais_detalhe = calculate_recipe_cost(df_finais, custo_total_dict, receita_col_name='NOME_BOLO')
    
    # 7. Compilar o DataFrame Final
    df_precificacao_final = pd.DataFrame(custo_finais_dict.items(), columns=['Produto', 'Custo Total de Insumos (R$)'])
    df_precificacao_final['Custo Total de Insumos (R$)'] = df_precificacao_final['Custo Total de Insumos (R$)'].round(2)
    df_precificacao_final = df_precificacao_final.sort_values(by='Custo Total de Insumos (R$)', ascending=False)
    
    # Retorna todos os dados para o frontend
    return df_precificacao_final, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict

# --- Streamlit App (Frontend) ---

def display_recipe_detail(selected_product, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict):
    """Mostra o detalhe completo da receita e custo do produto final."""
    
    st.subheader(f"Detalhe da Composição e Custo de Insumos: {selected_product}")
    
    # 1. Detalhe do Produto Final (NOME_BOLO)
    df_bolo = df_finais_detalhe[df_finais_detalhe['NOME_BOLO'] == selected_product].copy()
    
    # Prepara o DF para visualização
    df_bolo['Tipo de Item'] = df_bolo['NOME_INGREDIENTE'].apply(
        lambda x: 'Base' if x in custo_total_dict and x not in unidade_ingredientes_dict else 'Ingrediente Mestre/Final'
    )
    df_bolo['Custo Total (R$)'] = df_bolo['CUSTO_ITEM'].round(4)
    df_bolo['Custo Unitário'] = df_bolo['CUSTO_UNITARIO'].round(4)
    
    df_display = df_bolo[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Tipo de Item', 'CUSTO_UNITARIO', 'Custo Total (R$)']]
    df_display.columns = ['Item/Base Usada', 'Qtd na Receita', 'Tipo', 'Custo/Unidade Base (R$)', 'Custo Total do Item (R$)']
    
    st.dataframe(df_display, hide_index=True, use_container_width=True)
    
    total_custo = df_display['Custo Total do Item (R$)'].sum()
    st.metric("Custo Total do Produto (Insumos)", f"R$ {total_custo:,.2f}")
    
    # 2. Detalhe das Bases (Se houver)
    bases_usadas = df_bolo[df_bolo['Tipo de Item'] == 'Base']['NOME_INGREDIENTE'].unique()
    
    if bases_usadas.size > 0:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Os itens listados como 'Base' possuem um detalhamento de custo próprio, composto por ingredientes mestres.")
        
        for base in bases_usadas:
            st.markdown(f"#### Composição da Base: {base}")
            
            df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
            
            # Recupera o rendimento da base (se existir) para contextualizar o custo unitário
            rendimento = rendimento_bases.get(base, 1)
            custo_base_ajustado = custo_total_dict.get(base, 0)
            
            st.caption(f"Custo total da produção da Base {base}: R$ {df_base['CUSTO_ITEM'].sum():,.2f}. Rendimento: {rendimento} Unidades.")
            st.caption(f"Custo Ajustado por UNIDADE de Base usada no produto final: R$ {custo_base_ajustado:,.4f}.")
            
            df_base['Custo Total (R$)'] = df_base['CUSTO_ITEM'].round(4)
            df_base['Custo/Unidade Mestre (R$)'] = df_base['CUSTO_UNITARIO'].round(4)

            df_base_display = df_base[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
            df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
            
            st.dataframe(df_base_display, hide_index=True, use_container_width=True)

def main():
    st.set_page_config(page_title="Caderno de Receitas e Precificação 🍰", layout="wide")
    st.title("Caderno de Receitas e Precificação de Bolos")
    
    # --- 1. Carregar Dados ---
    with st.spinner('Ligando a IA da Precificação e buscando os dados no Sheets...'):
        try:
            df_precificacao_final, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict = get_all_calculated_data()
            all_products = df_precificacao_final['Produto'].tolist()
            
        except Exception as e:
            st.error(f"Não foi possível carregar ou calcular os dados. Verifique a planilha ou as Secrets. Erro: {e}")
            return
            
    st.success("Cálculos concluídos! Deslize para baixo ou comece sua consulta.")
    st.markdown("---")
    
    # --- 2. Interface de Consulta ---
    st.header("Consulta de Receitas e Custos")
    
    # Dropdown para consulta manual (Atende ao requisito)
    selected_product = st.selectbox(
        "Selecione o Bolo/Produto Final para Análise Detalhada:",
        options=["Selecione um Produto..."] + all_products
    )
    
    if selected_product == "Selecione um Produto...":
        st.info("Selecione um produto no menu suspenso acima para ver o custo, a receita e o detalhamento de cada ingrediente e base.")
        
        # Atende ao requisito de ver 'todos' os bolos (opcional)
        st.subheader("Visão Geral de Custo de Todos os Produtos")
        st.dataframe(df_precificacao_final, hide_index=True, use_container_width=True)
        return
        
    # Encontrou um produto
    else:
        # Usa abas para organizar a informação (Melhorando a UX)
        tab1, tab2 = st.tabs(["💰 Precificação (Custo Resumido)", "📋 Detalhe da Receita (Engenharia de Insumos)"])
        
        # --- TAB 1: CUSTO RESUMIDO ---
        with tab1:
            custo_produto = df_precificacao_final[df_precificacao_final['Produto'] == selected_product]['Custo Total de Insumos (R$)'].iloc[0]
            st.metric(f"Custo Total de Insumos para {selected_product}", f"R$ {custo_produto:,.2f}")
            
            # Espaço para o próximo aprimoramento (Margem de Lucro)
            st.markdown("""
            > **Próxima Etapa:** O custo de insumos é R$ **{custo_produto:,.2f}**. 
            Para chegar ao Preço de Venda ideal, aplique sua **Margem de Lucro**, 
            cubra seus custos fixos (aluguel, luz) e variáveis (gás, mão de obra).
            """)
            
        # --- TAB 2: DETALHE DA RECEITA ---
        with tab2:
            display_recipe_detail(selected_product, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict)

if __name__ == '__main__':
    # Esta parte é importante para garantir que a variável rendimento_bases seja globalmente acessível
    # para a função display_recipe_detail, mesmo que ela venha da função cacheada
    try:
        # Tenta carregar os dados de bases novamente, se necessário
        df_bases = load_data_from_gsheets('receitas_bases')
        df_rendimento = df_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates()
        df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1)
        rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
        
        main()
    except Exception as e:
        st.error(f"Erro Crítico na inicialização do app: {e}")
