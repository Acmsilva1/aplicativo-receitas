import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
# from dotenv import load_dotenv # Não é mais necessário para o Cloud

# --- Configurações Iniciais ---

# Variáveis do Google Sheets (Vem das Secrets do Streamlit)
SHEET_ID = os.getenv("SHEET_ID")
PRIVATE_KEY = os.getenv("GCP_SA_PRIVATE_KEY", "").replace("\\n", "\n")
CLIENT_EMAIL = os.getenv("GCP_SA_CLIENT_EMAIL")

# --- Funções de Conexão e Caching ---

@st.cache_resource
def get_service_account_credentials():
    """
    Constrói o JSON de credenciais a partir das variáveis de ambiente.
    CRÍTICO: Corrige o bug de variável de ambiente 'token_uri'.
    """
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
        st.error(f"Erro ao carregar dados da aba {sheet_name}. Verifique se o e-mail da Service Account tem acesso à planilha. Detalhes: {e}")
        st.stop()

# --- Funções de Processamento de Dados ---

def sanitize_and_convert(df, column_name):
    """Limpa e converte colunas de valores para float."""
    if column_name not in df.columns:
        return df 
        
    df[column_name] = df[column_name].astype(str).str.replace('R$', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.strip()
    df[column_name] = pd.to_numeric(df[column_name], errors='coerce').fillna(0.0)
    return df

def calculate_master_ingredient_cost(df_ingredientes):
    """Calcula o custo unitário (por G, ML ou UN) de cada ingrediente mestre."""
    
    df = df_ingredientes.copy()
    df = sanitize_and_convert(df, 'VALOR_PACOTE')
    
    df['QUANT_PACOTE'] = pd.to_numeric(df['QUANT_PACOTE'], errors='coerce').fillna(1).replace(0, 1)
    
    df['CUSTO_UNITARIO'] = df['VALOR_PACOTE'] / df['QUANT_PACOTE']
    
    df = df[['NOME_ITEM', 'UNIDADE_PACOTE', 'CUSTO_UNITARIO']]
    df.columns = ['NOME_INGREDIENTE', 'UNIDADE_BASE', 'CUSTO_UNITARIO']
    
    custo_dict = df.set_index('NOME_INGREDIENTE')['CUSTO_UNITARIO'].to_dict()
    unidade_dict = df.set_index('NOME_INGREDIENTE')['UNIDADE_BASE'].to_dict()
    
    return custo_dict, unidade_dict

def calculate_recipe_cost(df_receitas, custo_dict, receita_col_name):
    """Calcula o custo total de uma base ou receita final, e retorna o detalhe."""
    
    df = df_receitas.copy()
    df['QUANT_RECEITA'] = pd.to_numeric(df['QUANT_RECEITA'], errors='coerce').fillna(0)

    def calc_ingrediente_custo(row):
        nome_ingrediente = row['NOME_INGREDIENTE']
        quantidade_receita = row['QUANT_RECEITA']
        custo_unitario = custo_dict.get(nome_ingrediente)
        
        if custo_unitario is None:
            return 0.0
        
        return custo_unitario * quantidade_receita

    df['CUSTO_UNITARIO'] = df['NOME_INGREDIENTE'].apply(lambda x: custo_dict.get(x, 0.0))
    df['CUSTO_ITEM'] = df.apply(calc_ingrediente_custo, axis=1)

    custo_total_receita_dict = df.groupby(receita_col_name)['CUSTO_ITEM'].sum().to_dict()
    
    return custo_total_receita_dict, df 

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
    df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1).replace(0, 1)
    rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
    
    custo_bases_ajustado_dict = {}
    for base, custo in custo_bases_dict.items():
        rendimento = rendimento_bases.get(base, 1)
        custo_bases_ajustado_dict[base] = custo / rendimento # Custo por "unidade" de base produzida

    # 5. Compilar Custo Total (Ingredientes Mestres + Bases Ajustadas)
    custo_total_dict = {**custo_ingredientes_dict, **custo_bases_ajustado_dict}
    
    # 6. Calcular Custo das Receitas Finais
    custo_finais_dict, df_finais_detalhe = calculate_recipe_cost(df_finais, custo_total_dict, receita_col_name='NOME_BOLO')
    
    # 7. Compilar o DataFrame FINAL (incluindo Bases e Bolos Finais)
    
    # 7a. Receitas Finais
    df_receitas_finais = pd.DataFrame(custo_finais_dict.items(), columns=['Produto', 'Custo Total de Insumos (R$)'])
    df_receitas_finais['Tipo'] = 'Bolo Final (Especial)'

    # 7b. Bases (Bolos Comuns)
    df_bases_precificacao = pd.DataFrame(custo_bases_ajustado_dict.items(), columns=['Produto', 'Custo Total de Insumos (R$)'])
    df_bases_precificacao['Tipo'] = 'Bolo Comum (Base)'

    # 7c. Combinar todos os produtos para a lista de seleção (ATENDE AO REQUISITO)
    df_precificacao_completa = pd.concat([df_receitas_finais, df_bases_precificacao], ignore_index=True)
    df_precificacao_completa['Custo Total de Insumos (R$)'] = df_precificacao_completa['Custo Total de Insumos (R$)'].round(2)
    df_precificacao_completa = df_precificacao_completa.sort_values(by='Custo Total de Insumos (R$)', ascending=False)
    
    # Retorna todos os dados para o frontend, incluindo o rendimento das bases
    return df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases

# --- Streamlit App (Frontend) ---

def display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases):
    """Mostra o detalhe completo da receita e custo do produto final ou da base."""
    
    product_info = df_precificacao_completa[df_precificacao_completa['Produto'] == selected_product].iloc[0]
    product_type = product_info['Tipo']
    
    st.subheader(f"Composição e Custo de Insumos: {selected_product}")
    st.caption(f"Tipo de Produto: **{product_type}**")

    # --- Lógica para Bolo Final (usa Bases) ---
    if 'Bolo Final' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto é composto por Insumos Mestres e, possivelmente, Receitas Base (massas/coberturas).")
        
        df_bolo = df_finais_detalhe[df_finais_detalhe['NOME_BOLO'] == selected_product].copy()
        
        df_bolo['Tipo de Item'] = df_bolo['NOME_INGREDIENTE'].apply(
            lambda x: 'Base' if x in rendimento_bases else 'Ingrediente Mestre/Final'
        )
        df_bolo['Custo Total (R$)'] = df_bolo['CUSTO_ITEM'].round(4)
        df_bolo['Custo Unitário'] = df_bolo['CUSTO_UNITARIO'].round(4)
        
        df_display = df_bolo[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Tipo de Item', 'CUSTO_UNITARIO', 'Custo Total (R$)']]
        df_display.columns = ['Item/Base Usada', 'Qtd na Receita', 'Tipo', 'Custo/Unidade Base (R$)', 'Custo Total do Item (R$)']
        
        st.dataframe(df_display, hide_index=True, use_container_width=True)
        
        total_custo = df_display['Custo Total do Item (R$)'].sum()
        st.metric("Custo Total de Insumos", f"R$ {total_custo:,.2f}")
        
        bases_usadas = df_bolo[df_bolo['Tipo de Item'] == 'Base']['NOME_INGREDIENTE'].unique()
        
        if bases_usadas.size > 0:
            st.markdown("---")
            st.warning("🔎 **Rastreabilidade:** Detalhe dos custos de cada Base usada neste produto (rastreando até o ingrediente mestre).")
            
            for base in bases_usadas:
                st.markdown(f"#### Composição da Base: {base}")
                
                df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
                
                rendimento = rendimento_bases.get(base, 1)
                custo_base_ajustado = custo_total_dict.get(base, 0)
                
                st.caption(f"Custo total da produção da Base {base}: R$ {df_base['CUSTO_ITEM'].sum():,.2f}. Rendimento: {rendimento} Unidade(s).")
                st.caption(f"Custo Ajustado por UNIDADE de Base usada no produto final: R$ {custo_base_ajustado:,.4f}.")
                
                df_base['Custo Total (R$)'] = df_base['CUSTO_ITEM'].round(4)
                df_base['Custo/Unidade Mestre (R$)'] = df_base['CUSTO_UNITARIO'].round(4)
                
                # CORREÇÃO AQUI (no bloco do Bolo Final/Bases)
                df_base_display = df_base[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
                df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
                
                st.dataframe(df_base_display, hide_index=True, use_container_width=True)

    # --- Lógica para Bolo Comum (é uma Base) ---
    elif 'Bolo Comum' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto (massa pura) é composto **diretamente** por Insumos Mestres.")
        
        base = selected_product
        df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
        
        rendimento = rendimento_bases.get(base, 1)
        custo_base_ajustado = custo_total_dict.get(base, 0)
        
        st.caption(f"Custo total da produção da Base {base}: R$ {df_base['CUSTO_ITEM'].sum():,.2f}. Rendimento: {rendimento} Unidade(s).")
        st.caption(f"Custo Ajustado por UNIDADE (bolo/base) para o cálculo final: R$ {custo_base_ajustado:,.4f}.")
        
        df_base['Custo Total (R$)'] = df_base['CUSTO_ITEM'].round(4)
        df_base['Custo/Unidade Mestre (R$)'] = df_base['CUSTO_UNITARIO'].round(4)

        # CORREÇÃO AQUI (no bloco do Bolo Comum)
        df_base_display = df_base[['NOME_INGREDIENTE', 'QUANT_RECEITA', 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
        df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
        
        st.dataframe(df_base_display, hide_index=True, use_container_width=True)
        
        total_custo = df_base_display['Custo Total na Base (R$)'].sum() / rendimento
        st.metric("Custo Total do Produto (Insumos)", f"R$ {total_custo:,.2f}")

def main():
    st.set_page_config(page_title="Caderno de Receitas e Precificação 🍰", layout="wide")
    st.title("Caderno de Receitas e Precificação de Bolos")
    
    # --- 1. Carregar Dados ---
    with st.spinner('Ligando a IA da Precificação e buscando os dados no Sheets...'):
        try:
            df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases = get_all_calculated_data()
            all_products = df_precificacao_completa['Produto'].tolist()
            
        except Exception as e:
            st.error(f"Não foi possível carregar ou calcular os dados. Erro: {e}")
            return
            
    st.success("Cálculos concluídos! Deslize para baixo ou comece sua consulta.")
    st.markdown("---")
    
    # --- 2. Interface de Consulta ---
    st.header("Consulta de Receitas e Custos")
    
    selected_product = st.selectbox(
        "Selecione o Produto (Bolo Comum ou Especial) para Análise Detalhada:",
        options=["Selecione um Produto..."] + sorted(all_products)
    )
    
    if selected_product == "Selecione um Produto...":
        st.info("Selecione um produto no menu suspenso acima para ver o detalhe de custo e receita.")
        
        st.subheader("Visão Geral de Custo de Todos os Produtos")
        st.dataframe(df_precificacao_completa, hide_index=True, use_container_width=True)
        return
        
    # Encontrou um produto
    else:
        tab1, tab2 = st.tabs(["💰 Precificação (Custo Resumido)", "📋 Detalhe da Receita (Engenharia de Insumos)"])
        
        # --- TAB 1: CUSTO RESUMIDO ---
        with tab1:
            custo_produto = df_precificacao_completa[df_precificacao_completa['Produto'] == selected_product]['Custo Total de Insumos (R$)'].iloc[0]
            st.metric(f"Custo Total de Insumos para {selected_product}", f"R$ {custo_produto:,.2f}")
            
            st.markdown(f"""
            > **Próxima Etapa:** O custo de insumos é R$ **{custo_produto:,.2f}**. 
            Vamos integrar a calculadora de Markup/Margem para obter o Preço de Venda Final, André.
            """)
            
        # --- TAB 2: DETALHE DA RECEITA ---
        with tab2:
            display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases)

if __name__ == '__main__':
    main()
