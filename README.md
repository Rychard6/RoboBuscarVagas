# Monitor de vagas — dois radares independentes

Bot Python que pesquisa resultados públicos do Google por meio do Serper, valida vagas para dois perfis independentes e envia alertas pelo Telegram. O GitHub Actions executa a busca a cada quatro horas; não é necessário contratar infraestrutura ou adicionar outro serviço.

## Radares

- **Radar A — Segunda Renda:** aceita CLT, PJ e estágio; remoto, híbrido ou presencial. Vagas remotas devem ser destinadas a candidatos no Brasil. Vagas híbridas/presenciais precisam ser confirmadas no Distrito Federal; Entorno, Goiás e localizações identificadas fora do DF são rejeitados. Prioriza jornadas noturnas em TI.
- **Radar B — Carreira em Tecnologia:** somente remoto e contrato PJ ou estágio. Aceita estágio, Júnior e Pleno; Sênior somente em função explicitamente Front-end. Vagas CLT, híbridas ou presenciais são rejeitadas antes da pontuação. Horário comercial recebe bônus, sem excluir outras jornadas.

No Radar A, contrato ou modalidade ausentes não eliminam o resultado: ele fica pendente de confirmação. Uma vaga remota sem abrangência brasileira confirmada e uma vaga local sem DF confirmado também ficam pendentes; localização explicitamente fora do DF é rejeitada. No Radar B, modalidade, contrato permitido, área e senioridade precisam ser confirmados; informações ausentes ficam pendentes, mas CLT, modalidade não remota e senioridade incompatível são rejeitadas. O bot não presume PJ pela ausência de CLT nem estágio por baixa experiência. A mesma vaga pode aparecer nos dois radares, mas é agrupada em uma notificação com resultados independentes.

O horário é classificado como **Noturno confirmado**, **Horário comercial**, **Misto** ou **Horário não informado**. Menções explícitas a noite/madrugada e jornadas que cruzam a meia-noite são reconhecidas; faixas entre 22:00 e 05:00 contam como noturnas. Escala 12x36 sem horário não é presumida noturna. A data/hora de publicação nunca é usada para inferir jornada. Noturno confirmado em TI recebe prioridade muito alta no Radar A; o Radar B mantém prioridade alta para PJ remoto e estágio remoto em TI, com bônus para horário comercial. Pontuação não substitui os critérios de elegibilidade.

Quando um resultado está incompleto, o bot pode consultar a página original HTTPS em domínios configurados, sem seguir redirecionamentos e com limite de requisições e tamanho. Se ainda não conseguir confirmar os dados, a oportunidade permanece pendente e é identificada como tal na notificação. Rejeições registram título, URL e motivo no log da execução; as métricas mostram totais por radar e por motivo. Compatibilidade técnica e potencial de carreira dependem de evidências encontradas na vaga. Salário ausente aparece como **“Não divulgado”**.

Pontuações e limites de prioridade estão em `sites_config.yaml`. Consultas e fontes públicas estão em `google_search_sites.yaml`. Ajuste as listas de termos de acordo com o perfil; as regras de pontuação e validação ficam em `radar_logic.py`.

## Configuração

Adicione estes secrets em **Settings → Secrets and variables → Actions**:

| Secret | Obrigatório | Descrição |
|---|---:|---|
| `SERPER_API_KEY` | Sim | Chave do Serper.dev |
| `TELEGRAM_BOT_TOKEN` | Sim | Token do bot criado com @BotFather |
| `TELEGRAM_CHAT_ID` | Sim | Chat que receberá as mensagens |

As consultas definem português (`pt-br`) e Brasil (`br`) como idioma e viés de busca. Isso orienta o Google, mas **não comprova** que a vaga seja elegível; a validação é feita separadamente pelo conteúdo do resultado e, quando necessário, pelos dados disponíveis na página original.

## Execução local

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SERPER_API_KEY = "sua-chave"
$env:TELEGRAM_BOT_TOKEN = "seu-token"
$env:TELEGRAM_CHAT_ID = "seu-chat-id"
python job_monitor.py --dry-run
```

`--dry-run` pesquisa e mostra resultados sem enviar mensagens nem persistir alterações no histórico.

## Histórico e status

O histórico mantém a URL canônica, fontes, publicação, pontuação e status de entrega por radar; a mesma URL encontrada em várias consultas/fontes é agrupada em uma única notificação. IDs de vagas anteriores no formato legado continuam sendo reconhecidos.

Status de acompanhamento disponíveis: `interessada`, `candidatura_enviada`, `entrevista`, `recusada` e `encerrada`.

```powershell
python job_monitor.py --history
python job_monitor.py --set-status ID_DA_VAGA radar_a entrevista
```

O ID curto é incluído na notificação. `--history` lista título e URL e, separadamente para cada radar, exibe status de acompanhamento, confirmação e motivo quando estiver pendente. Atualize um radar por vez (`radar_a` ou `radar_b`); registros legados com status global são migrados para os radares existentes sem sobrescrever status individuais já gravados. O histórico mantém a trilha de alterações de status separadamente para cada radar. No GitHub Actions, execute manualmente o workflow e escolha **history** para listar o histórico ou **set-status** para atualizar um status.

O histórico continua no cache gratuito do GitHub Actions, não em armazenamento durável. Restauração e salvamento usam `seen_jobs.json` e a mesma família de chaves. A gravação fica no escopo da branch da execução, incluindo `developer`; se não houver correspondência nesse escopo, o prefixo de restauração pode usar o cache da branch padrão. O cache tem retenção limitada, pode expirar ou ser removido pelo GitHub e não equivale a armazenamento permanente; uma perda pode causar notificações repetidas e perda do histórico/status. As chaves incluem o número da tentativa (`run_attempt`) e cada execução restaura pelo prefixo para evitar que um rerun reutilize uma chave de gravação imutável. Isso reduz colisões, mas não elimina perdas do cache nem oferece garantias de transação/backup. A opção **history** mostra o estado restaurado no Actions; a execução local mostra o `seen_jobs.json` disponível naquela cópia.

## Notificações e cadência

- Vagas de alta prioridade (80–100) são notificadas logo após a busca.
- Média (60–79) e baixa (abaixo de 60) ficam pendentes para o resumo diário.
- O resumo diário e o heartbeat estão temporariamente programados para **00:30 UTC (21:30 em Brasília)** para teste. O estado persiste a data do último resumo para impedir repetição no mesmo dia; a execução manual não envia o resumo diário.
- Cada mensagem inclui radar, pontuação/prioridade, contrato, modalidade, localidade, turno, salário, tecnologias, requisitos, compatibilidade, pontos de atenção, data, status e link original.

Há seis grupos de busca por execução, com até 10 resultados por consulta. O cron mantém as buscas recorrentes a cada quatro horas (`55 3,7,11,15,19,23 * * *`) e acrescenta temporariamente uma execução diária às 00:30 UTC (`30 0 * * *`) para testar o resumo. O saldo gratuito do Serper é limitado; reduza grupos ou frequência se necessário. Nenhum scraping autenticado, CAPTCHA ou endpoint privado é utilizado.

## Testes

```powershell
python -m unittest -v
```

Os testes cobrem regras obrigatórias, pontuação/prioridade, horários não inferidos, migração do estado legado, deduplicação entre radares e atualização de status.
