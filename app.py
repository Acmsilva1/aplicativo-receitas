import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
import numpy as np 

# --- Configurações Iniciais ---

SHEET_ID = os.getenv("SHEET_ID")
# A variável de ambiente PRIVATE_KEY deve ser configurada no Streamlit Secrets
PRIVATE_KEY = os.getenv("GCP_SA_PRIVATE_KEY", "").replace("\\n", "\n")
CLIENT_EMAIL = os.getenv("GCP_SA_CLIENT_EMAIL")

# --- Funções de Conexão e Caching (Sem Alteração na Conexão) ---

@st.cache_resource
def get_service_account_credentials():
    """
    Constrói o JSON de credenciais a partir das variáveis de ambiente.
    """
    if not all([CLIENT_EMAIL, PRIVATE_KEY]):
        st.error("Erro de configuração: Credenciais do Google Cloud não encontradas.")
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

@st.cache_data(ttl=600)
def load_data_from_gsheets(sheet_name):
    """Conecta ao Google Sheets e carrega os dados de uma aba específica."""
    try:
        creds = get_service_account_credentials()
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(sheet_name)
        
        data = worksheet.get_all_records()
        df = pd.DataFrame(data)
        
        # Converte nomes de colunas para maiúsculas e remove espaços
        df.columns = [col.upper().strip() for col in df.columns]
        
        return df
    
    except Exception as e:
        st.error(f"Erro ao carregar dados da aba {sheet_name}. Verifique se o e-mail da Service Account tem acesso à planilha. Detalhes: {e}")
        st.stop()
        
def sanitize_and_convert(df, column_name):
    """Limpa e converte colunas de valores para float."""
    if column_name not in df.columns:
        return df 
        
    # Lógica de limpeza para R$ (aplica-se ao custo/preço)
    if 'VALOR_PACOTE' in column_name.upper() or 'PRECO_VENDA' in column_name.upper():
         df[column_name] = df[column_name].astype(str).str.replace('R$', '', regex=False).str.replace('.', '', regex=False).str.replace(',', '.', regex=False).str.strip()
         
    df[column_name] = pd.to_numeric(df[column_name], errors='coerce').fillna(0.0)
    return df

# --- NOVAS Funções de Processamento de Dados (Gerais) ---

def calculate_master_data(df_ingredientes, col_valor, new_dict_name):
    """
    Calcula o valor unitário (Custo, Kcal, Proteína, etc.) de cada ingrediente mestre.
    """
    df = df_ingredientes.copy()
    
    # 1. Limpa e Converte (aplica R$ se for custo)
    df = sanitize_and_convert(df, col_valor)
    
    # 2. Garante que QUANT_PACOTE seja um número positivo
    df['QUANT_PACOTE'] = pd.to_numeric(df['QUANT_PACOTE'], errors='coerce').fillna(1).replace(0, 1)
    
    # 3. Cálculo do Valor Unitário
    if col_valor == 'VALOR_PACOTE':
        # Para custo: Valor do Pacote / Quantidade do Pacote
        df[new_dict_name] = df[col_valor] / df['QUANT_PACOTE']
    else:
        # Para Nutrientes (KCAL, PROTEINA_G, etc): assume-se que o valor já é por G/ML/UN
        df[new_dict_name] = df[col_valor] 

    # 4. Formata o DF de saída
    df = df[['NOME_ITEM', 'UNIDADE_PACOTE', new_dict_name]]
    df.columns = ['NOME_INGREDIENTE', 'UNIDADE_BASE', new_dict_name]
    
    data_dict = df.set_index('NOME_INGREDIENTE')[new_dict_name].to_dict()
    unidade_dict = df.set_index('NOME_INGREDIENTE')['UNIDADE_BASE'].to_dict()
    
    return data_dict, unidade_dict

def calculate_recipe_general(df_receitas, unit_value_dict, receita_col_name, unit_value_col):
    """
    Calcula o valor total (Custo ou Nutriente) de uma base ou receita final, e retorna o detalhe.
    """
    
    df = df_receitas.copy()
    df['QUANT_RECEITA'] = pd.to_numeric(df['QUANT_RECEITA'], errors='coerce').fillna(0)

    def calc_item_total_value(row):
        nome_ingrediente = row['NOME_INGREDIENTE']
        quantidade_receita = row['QUANT_RECEITA']
        unit_value = unit_value_dict.get(nome_ingrediente)
        
        if unit_value is None:
            # Garante que ingredientes inexistentes no mestre não quebrem o cálculo
            return 0.0
        
        return unit_value * quantidade_receita

    df[unit_value_col] = df['NOME_INGREDIENTE'].apply(lambda x: unit_value_dict.get(x, 0.0))
    df['VALOR_TOTAL_ITEM'] = df.apply(calc_item_total_value, axis=1)

    total_value_dict = df.groupby(receita_col_name)['VALOR_TOTAL_ITEM'].sum().to_dict()
    
    # Renomeia as colunas de retorno para o uso no detalhe
    df.rename(columns={
        unit_value_col: 'VALOR_UNITARIO_ITEM', 
        'VALOR_TOTAL_ITEM': 'VALOR_TOTAL_CALCULADO'
    }, inplace=True)
    
    return total_value_dict, df 

# --- Funções de Cálculo Específicas (Custo) ---

def get_calculated_cost_data(df_ingredientes, df_bases, df_finais, df_precos_mercado):
    """Calcula e compila todos os dados de CUSTO e MARGEM."""
    
    # 1. Calcular Custos de Ingredientes Mestres
    custo_ingredientes_dict, unidade_ingredientes_dict = calculate_master_data(
        df_ingredientes, 'VALOR_PACOTE', 'CUSTO_UNITARIO'
    )
    
    # 2. Calcular Custos nas Bases
    custo_bases_dict, df_bases_detalhe = calculate_recipe_general(
        df_bases, custo_ingredientes_dict, receita_col_name='NOME_BASE', unit_value_col='CUSTO_UNITARIO'
    )
    
    # 3. Ajuste de Rendimento (Custo por Unidade de Base Final)
    df_rendimento = df_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates()
    df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1).replace(0, 1)
    rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()
    
    custo_bases_ajustado_dict = {}
    for base, custo in custo_bases_dict.items():
        rendimento = rendimento_bases.get(base, 1)
        # Custo total da base / Rendimento = Custo unitário da base
        custo_bases_ajustado_dict[base] = custo / rendimento
        
    custo_total_dict = {**custo_ingredientes_dict, **custo_bases_ajustado_dict}
    
    # 4. Calcular Custos nas Receitas Finais
    custo_finais_dict, df_finais_detalhe = calculate_recipe_general(
        df_finais, custo_total_dict, receita_col_name='NOME_BOLO', unit_value_col='CUSTO_UNITARIO'
    )
    
    # 5. Compilar o DataFrame FINAL de CUSTO (Incluindo Bases e Receitas Finais)
    df_receitas_finais = pd.DataFrame(custo_finais_dict.items(), columns=['PRODUTO', 'Custo Total de Insumos (R$)'])
    df_receitas_finais['Tipo'] = 'Bolo Final (Especial)'

    df_bases_precificacao = pd.DataFrame(custo_bases_ajustado_dict.items(), columns=['PRODUTO', 'Custo Total de Insumos (R$)'])
    df_bases_precificacao['Tipo'] = 'Bolo Comum (Base)'

    df_precificacao_completa = pd.concat([df_receitas_finais, df_bases_precificacao], ignore_index=True)
    df_precificacao_completa['Custo Total de Insumos (R$)'] = df_precificacao_completa['Custo Total de Insumos (R$)'].round(2)
    
    # 6. Merge com os preços de venda fixos
    df_precificacao_completa = pd.merge(
        df_precificacao_completa, 
        df_precos_mercado[['PRODUTO', 'PRECO_VENDA_FINAL']], 
        on='PRODUTO', 
        how='left'
    )
    
    df_precificacao_completa.rename(columns={'PRECO_VENDA_FINAL': 'Preço de Venda (Mercado) (R$)'}, inplace=True)
    df_precificacao_completa['Preço de Venda (Mercado) (R$)'] = df_precificacao_completa['Preço de Venda (Mercado) (R$)'].fillna(0.0)
    
    # 7. Calcular o Lucro Bruto (R$) e a Margem Percentual
    
    df_precificacao_completa['Custo Total de Insumos (R$)'] = df_precificacao_completa['Custo Total de Insumos (R$)'].replace(0, np.nan) 
    
    df_precificacao_completa['Lucro Bruto (R$)'] = (
        df_precificacao_completa['Preço de Venda (Mercado) (R$)'] - df_precificacao_completa['Custo Total de Insumos (R$)']
    ).replace([np.inf, -np.inf], np.nan).fillna(0.0).round(2)
    
    df_precificacao_completa['Margem Bruta (%)'] = (
        df_precificacao_completa['Lucro Bruto (R$)'] / 
        df_precificacao_completa['Preço de Venda (Mercado) (R$)']
    ).replace([np.inf, -np.inf], np.nan).fillna(0.0) * 100
    
    df_precificacao_completa['Margem Bruta (%)'] = df_precificacao_completa['Margem Bruta (%)'].round(1)

    return df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, rendimento_bases, unidade_ingredientes_dict

# --- NOVAS Funções de Cálculo Específicas (Nutrição) ---

def get_calculated_nutrition_data(df_ingredientes, df_bases, df_finais):
    """
    Calcula e compila todos os dados NUTRICIONAIS (KCAL, Proteína, Carbo, Gordura, Fibra, Sódio)
    para Receitas Finais e Bases.
    """
    
    # NOVAS COLUNAS: Certifique-se de que estas colunas existem no seu 'ingredientes_mestres'
    NUTRIENTES = {
        'KCAL': 'Kcal Totais',
        'PROTEINA_G': 'Proteína (g)', 
        'CARBO_G': 'Carboidrato (g)', 
        'GORDURA_G': 'Gordura (g)',
        'FIBRA_G': 'Fibra (g)',
        'SODIO_MG': 'Sódio (mg)'
    }
    
    # Reutiliza o rendimento do cálculo de custo
    df_rendimento = df_bases[['NOME_BASE', 'RENDIMENTO_FINAL_UNIDADES']].drop_duplicates()
    df_rendimento['RENDIMENTO_FINAL_UNIDADES'] = pd.to_numeric(df_rendimento['RENDIMENTO_FINAL_UNIDADES'], errors='coerce').fillna(1).replace(0, 1)
    rendimento_bases = df_rendimento.set_index('NOME_BASE')['RENDIMENTO_FINAL_UNIDADES'].to_dict()

    df_nutrition_final = None

    for col_nutriente, display_name in NUTRIENTES.items():
        # Verifica se a coluna existe na fonte de dados
        if col_nutriente not in df_ingredientes.columns:
            # Continua o loop para os outros nutrientes
            continue 

        # 1. Calcular Nutrientes Mestres (Valor por G/ML/UN)
        nutriente_ingredientes_dict, _ = calculate_master_data(
            df_ingredientes, col_nutriente, col_nutriente
        )
        
        # 2. Calcular Nutrientes nas Bases (Total da Receita Base)
        nutriente_bases_dict, _ = calculate_recipe_general(
            df_bases, nutriente_ingredientes_dict, receita_col_name='NOME_BASE', unit_value_col=f'{col_nutriente}_UNIT'
        )
        
        # 3. Ajuste de Rendimento (Nutriente por Unidade de Base Final)
        nutriente_bases_ajustado_dict = {}
        for base, nutriente in nutriente_bases_dict.items():
            rendimento = rendimento_bases.get(base, 1)
            # Nutriente total da base / Rendimento = Nutriente unitário da base
            nutriente_bases_ajustado_dict[base] = nutriente / rendimento
            
        nutriente_total_dict = {**nutriente_ingredientes_dict, **nutriente_bases_ajustado_dict}
        
        # 4. Calcular Nutrientes nas Receitas Finais (Total da Receita Final)
        nutriente_finais_dict, _ = calculate_recipe_general(
            df_finais, nutriente_total_dict, receita_col_name='NOME_BOLO', unit_value_col=f'{col_nutriente}_UNIT'
        )

        # 5. COMPILAR RESULTADOS: BASES + FINAIS JUNTOS
        
        # Dataframe para Receitas Base (com valor por unidade)
        df_nutri_bases_temp = pd.DataFrame(nutriente_bases_ajustado_dict.items(), columns=['PRODUTO', display_name])
        # Dataframe para Receitas Finais (com valor por unidade)
        df_nutri_finais_temp = pd.DataFrame(nutriente_finais_dict.items(), columns=['PRODUTO', display_name])
        
        # Concatena bases e finais
        df_nutri_temp = pd.concat([df_nutri_bases_temp, df_nutri_finais_temp], ignore_index=True)
        df_nutri_temp[display_name] = df_nutri_temp[display_name].round(2)
        
        # Merge para construir o DF final com todas as colunas de nutrientes
        if df_nutrition_final is None:
            df_nutrition_final = df_nutri_temp[['PRODUTO', display_name]]
        else:
            df_nutrition_final = pd.merge(df_nutrition_final, df_nutri_temp[['PRODUTO', display_name]], on='PRODUTO', how='outer')

    return df_nutrition_final, NUTRIENTES
    
@st.cache_data(ttl=600)
def get_all_calculated_data():
    """Carrega todos os dados e calcula CUSTO e NUTRIÇÃO."""
    
    # 1. Carregar Dados Brutos
    df_ingredientes = load_data_from_gsheets('ingredientes_mestres')
    df_bases = load_data_from_gsheets('receitas_bases')
    df_finais = load_data_from_gsheets('receitas_finais')
    
    # 2. Carregar e Limpar Preços de Mercado (para cálculo de custo)
    df_precos_mercado_bruto = load_data_from_gsheets('tabela_precos_mercado')
    COL_PRODUTO_KEY = 'PRODUTO'
    colunas_disponiveis = df_precos_mercado_bruto.columns.tolist()

    if len(colunas_disponiveis) < 2:
        st.error("A aba 'tabela_precos_mercado' deve ter pelo menos duas colunas (PRODUTO e a coluna de preço de venda).")
        st.stop()
        
    COL_PRECO_KEY = colunas_disponiveis[1]

    df_precos_mercado = df_precos_mercado_bruto[[COL_PRODUTO_KEY, COL_PRECO_KEY]].copy()
    df_precos_mercado.rename(columns={COL_PRECO_KEY: 'PRECO_VENDA_FINAL', COL_PRODUTO_KEY: 'PRODUTO'}, inplace=True)
    df_precos_mercado = sanitize_and_convert(df_precos_mercado, 'PRECO_VENDA_FINAL')
    
    # 3. Calcular Custos e Margens (Retorna DF unificado de Bases e Finais)
    df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, rendimento_bases, unidade_ingredientes_dict = get_calculated_cost_data(
        df_ingredientes, df_bases, df_finais, df_precos_mercado
    )
    
    # 4. Calcular Nutrição (Retorna DF unificado de Bases e Finais)
    df_nutrition_final, NUTRIENTES = get_calculated_nutrition_data(df_ingredientes, df_bases, df_finais)
    
    # 5. Mergear Nutrição com Precificação no DataFrame Principal
    if df_nutrition_final is not None:
        df_precificacao_completa = pd.merge(
            df_precificacao_completa, 
            df_nutrition_final, 
            on='PRODUTO', 
            how='left'
        )

    # Ordenação final
    df_precificacao_completa = df_precificacao_completa.sort_values(by='Preço de Venda (Mercado) (R$)', ascending=False)
    
    return df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases, NUTRIENTES


# --- Streamlit App (Frontend) ---

def display_nutrition_analysis(selected_product, df_precificacao_completa, NUTRIENTES):
    """Mostra o detalhe nutricional do produto final ou da base."""
    
    st.subheader(f"Tabela Nutricional (Estimada): {selected_product}")
    product_type = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]['Tipo']
    
    # A base e o produto final usam o mesmo DF principal
    st.info(f"⚠️ Os valores são para **1 UNIDADE** do produto ({product_type}) e são estimados com base na soma dos insumos (não consideram perdas no cozimento).")
    
    product_data = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]
    
    # Prepara a tabela de visualização
    data = []
    
    # Garante que só mostre nutrientes que foram calculados (i.e., que têm a coluna no Sheet)
    for col_nutriente, display_name in NUTRIENTES.items():
        if display_name in product_data:
            valor = product_data[display_name]
            # Define a unidade de exibição
            unidade = '(Kcal)' if 'Kcal' in display_name else ('(mg)' if 'Sódio' in display_name else '(g)')
            
            data.append({
                'Nutriente': display_name.replace(unidade, '').strip(),
                'Valor Total': f"{valor:,.2f}",
                'Unidade': unidade
            })
            
    df_display = pd.DataFrame(data)
    
    if df_display.empty:
        st.warning("Nenhum dado nutricional encontrado. Verifique se as novas colunas (KCAL, PROTEINA_G, etc.) foram adicionadas e preenchidas no Sheet `ingredientes_mestres`.")
        return

    # Kcal em destaque
    kcal_value = product_data.get('Kcal Totais', 0.0)
    st.metric("Total de Calorias (Estimado)", f"{kcal_value:,.0f} Kcal")
    st.markdown("---")
    
    # Tabela detalhada
    st.dataframe(df_display, hide_index=True, use_container_width=True)


def display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases):
    """Mostra o detalhe completo da receita e custo do produto final ou da base."""
    
    product_info = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]
    product_type = product_info['Tipo']
    
    st.subheader(f"Composição e Custo de Insumos: {selected_product}")
    st.caption(f"Tipo de Produto: **{product_type}**")

    # Colunas de detalhe unificadas
    COL_INGREDIENTE = 'NOME_INGREDIENTE'
    COL_QTD = 'QUANT_RECEITA'
    COL_CUSTO_UNIT = 'VALOR_UNITARIO_ITEM' # CUSTO/UNIDADE MESTRE
    COL_CUSTO_TOTAL = 'VALOR_TOTAL_CALCULADO' # CUSTO TOTAL NA RECEITA

    if 'Bolo Final' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto é composto por Insumos Mestres e, possivelmente, Receitas Base.")
        
        # Filtra o DF DETALHE de CUSTO
        df_bolo = df_finais_detalhe[df_finais_detalhe['NOME_BOLO'] == selected_product].copy()
        
        df_bolo['Tipo de Item'] = df_bolo[COL_INGREDIENTE].apply(
            lambda x: 'Base' if x in rendimento_bases else 'Ingrediente Mestre/Final'
        )
        df_bolo['Custo Total (R$)'] = df_bolo[COL_CUSTO_TOTAL].round(4)
        
        df_display = df_bolo[[COL_INGREDIENTE, COL_QTD, 'Tipo de Item', COL_CUSTO_UNIT, 'Custo Total (R$)']]
        df_display.columns = ['Item/Base Usada', 'Qtd na Receita', 'Tipo', 'Custo/Unidade Base (R$)', 'Custo Total do Item (R$)']
        
        st.dataframe(df_display, hide_index=True, use_container_width=True)
        
        total_custo = df_display['Custo Total do Item (R$)'].sum()
        st.metric("Custo Total de Insumos", f"R$ {total_custo:,.2f}")
        
    elif 'Bolo Comum' in product_type:
        st.markdown("---")
        st.info("💡 **Análise de Dados:** Este produto (massa pura) é composto **diretamente** por Insumos Mestres.")
        
        base = selected_product
        df_base = df_bases_detalhe[df_bases_detalhe['NOME_BASE'] == base].copy()
        
        rendimento = rendimento_bases.get(base, 1)
        custo_base_ajustado = custo_total_dict.get(base, 0)
        
        st.caption(f"Custo total da produção da Base {base}: R$ {df_base[COL_CUSTO_TOTAL].sum():,.2f}. Rendimento: {rendimento} Unidade(s).")
        st.caption(f"Custo Ajustado por UNIDADE (bolo/base) para o cálculo final: R$ {custo_base_ajustado:,.4f}.")
        
        df_base['Custo Total (R$)'] = df_base[COL_CUSTO_TOTAL].round(4)
        df_base['Custo/Unidade Mestre (R$)'] = df_base[COL_CUSTO_UNIT].round(4)

        df_base_display = df_base[[COL_INGREDIENTE, COL_QTD, 'Custo/Unidade Mestre (R$)', 'Custo Total (R$)']]
        df_base_display.columns = ['Ingrediente Mestre', 'Qtd na Receita (G/ML/UN)', 'Custo/Unidade (R$)', 'Custo Total na Base (R$)']
        
        st.dataframe(df_base_display, hide_index=True, use_container_width=True)
        
        total_custo = df_base[COL_CUSTO_TOTAL].sum() / rendimento
        st.metric("Custo Total do Produto (Insumos)", f"R$ {total_custo:,.2f}")

def main():
    st.set_page_config(page_title="Caderno de Receitas, Custo e Nutrição 🍰", layout="wide")
    st.title("Caderno de Receitas, Custo e Análise Nutricional")
    
    # --- 1. Carregar Dados ---
    with st.spinner('Ligando a IA da Precificação e Nutrição e buscando os dados no Sheets...'):
        try:
            df_precificacao_completa, custo_total_dict, df_bases_detalhe, df_finais_detalhe, unidade_ingredientes_dict, rendimento_bases, NUTRIENTES = get_all_calculated_data()
            all_products = df_precificacao_completa['PRODUTO'].tolist()
            
        except Exception as e:
            st.error(f"Não foi possível carregar ou calcular os dados. Erro: {e}")
            
            # Guia de Debugging para o usuário
            st.markdown("---")
            st.subheader("Checklist de Conexão e Dados (Se o erro persistir)")
            st.error("""
            1. **Google Sheets:** Todas as abas (`ingredientes_mestres`, `receitas_bases`, etc.) estão com os nomes EXATAMENTE corretos?
            2. **Novas Colunas:** As colunas **KCAL, PROTEINA_G, CARBO_G, GORDURA_G, FIBRA_G, SODIO_MG** foram adicionadas e preenchidas (com valores por G/ML/UN) na aba `ingredientes_mestres`?
            3. **Permissão:** O e-mail da Service Account (GCP_SA_CLIENT_EMAIL) tem permissão de LEITURA na planilha?
            """)
            
            return
            
    st.success("Cálculos concluídos! Deslize para baixo ou comece sua consulta.")
    st.markdown("---")
    
    # --- 2. Interface de Consulta ---
    st.header("Análise de Preço e Nutrição")
    
    selected_product = st.selectbox(
        "Selecione o Produto (Final ou Base) para Análise Detalhada:",
        options=["Selecione um Produto..."] + sorted(all_products)
    )
    
    if selected_product == "Selecione um Produto...":
        st.info("Selecione um produto para começar a analisar Custo, Lucro e Nutrição.")
        
        st.subheader("Visão Geral de Lucro Bruto e Nutrição Principal")
        
        # Tabela resumo, incluindo KCAL
        cols_summary = ['PRODUTO', 'Tipo', 'Custo Total de Insumos (R$)', 'Preço de Venda (Mercado) (R$)', 'Lucro Bruto (R$)', 'Margem Bruta (%)']
        
        # Adiciona Kcal Totais se a coluna estiver presente no DF
        if 'Kcal Totais' in df_precificacao_completa.columns:
            cols_summary.append('Kcal Totais')
            
        df_display_summary = df_precificacao_completa[cols_summary]
        
        # Renomeia colunas para exibição
        display_names = ['Produto', 'Tipo', 'Custo Insumos (R$)', 'Preço de Venda (R$)', 'Lucro Bruto (R$)', 'Margem Bruta (%)']
        if 'Kcal Totais' in cols_summary:
            display_names.append('Kcal Totais')
            
        df_display_summary.columns = display_names
        
        st.dataframe(df_display_summary, hide_index=True, use_container_width=True)
        return
        
    # Encontrou um produto
    else:
        # TABS: 1. CUSTO/PREÇO, 2. NUTRICIONAL, 3. DETALHE DA RECEITA
        tab1, tab2, tab3 = st.tabs(["📊 Análise de Lucro e Margem", "📝 Análise Nutricional", "📋 Detalhe da Receita (Engenharia de Insumos)"])
        
        # --- TAB 1: CUSTO E PREÇO FINAL ---
        with tab1:
            product_data = df_precificacao_completa[df_precificacao_completa['PRODUTO'] == selected_product].iloc[0]
            
            custo_produto = product_data['Custo Total de Insumos (R$)']
            preco_venda = product_data['Preço de Venda (Mercado) (R$)']
            lucro_bruto = float(product_data['Lucro Bruto (R$)'])
            margem_percentual = float(product_data['Margem Bruta (%)'])

            col1, col2, col3 = st.columns(3)
            col1.metric("Custo Total de Insumos (Seu Custo)", f"R$ {custo_produto:,.2f}")
            
            # Se for base, não mostra preço de venda, que deve ser 0.0
            if product_data['Tipo'] == 'Bolo Comum (Base)':
                col2.metric("Preço de Venda (Mercado)", "N/A (Base)")
                col3.metric("Lucro Bruto (R$)", "N/A (Base)")
                st.info("Este é um item base, o lucro/margem é calculado apenas nos produtos finais que o utilizam.")
            else:
                col2.metric("Preço de Venda (Seu Mercado)", f"R$ {preco_venda:,.2f}")
                
                col3.metric(
                    label="Lucro Bruto (R$)", 
                    value=f"R$ {lucro_bruto:,.2f}", 
                    delta=lucro_bruto,
                    delta_color='normal'
                )
                
                margem_color = '' 
                if margem_percentual > 40:
                    margem_color = "🟢 **Excelente**"
                elif margem_percentual >= 20:
                    margem_color = "🟡 **Razoável**"
                else:
                    margem_color = "🔴 **Baixa**"
                
                st.markdown("---")
                st.markdown(f"#### Margem de Lucro Bruta: {margem_color}")
                st.subheader(f"**{margem_percentual:,.1f} %**")
            
            st.markdown("---")
            st.info("""
                **(Lembrete LGPD/Governança): A estrutura de dados e cálculos é mantida neutra, focando estritamente em insumos e receitas, sem processamento de dados pessoais.**
            """)

        # --- TAB 2: ANÁLISE NUTRICIONAL (NOVA) ---
        with tab2:
            display_nutrition_analysis(selected_product, df_precificacao_completa, NUTRIENTES)

        # --- TAB 3: DETALHE DA RECEITA (CUSTO) ---
        with tab3:
            display_recipe_detail(selected_product, df_precificacao_completa, df_finais_detalhe, custo_total_dict, df_bases_detalhe, unidade_ingredientes_dict, rendimento_bases)

if __name__ == '__main__':
    main()
