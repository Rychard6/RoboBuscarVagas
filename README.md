# Monitor de vagas — dois radares independentes

Bot Python que pesquisa resultados públicos do Google por meio do Serper, valida vagas para dois perfis independentes e envia alertas pelo Telegram. O GitHub Actions executa a busca a cada quatro horas; não é necessário contratar infraestrutura ou adicionar outro serviço.

## Radares

- **Radar A — Segunda Renda:** CLT, PJ ou estágio; remoto, híbrido ou presencial. Trabalho remoto precisa indicar abrangência no Brasil; vagas híbridas/presenciais precisam indicar Brasília/DF ou uma localidade próxima configurada. Prioriza turno noturno, madrugada, overnight e escalas compatíveis.
- **Radar B — Carreira em Tecnologia:** somente remoto e contrato PJ ou estágio. Aceita estágio, Júnior e Pleno; Sênior somente em função explicitamente Front-end. Vagas CLT, híbridas ou presenciais são rejeitadas antes da pontuação.

Os dois radares precisam confirmar contrato e modalidade nos dados públicos encontrados. O Radar B também exige senioridade identificável. Sinais ausentes, negados, contraditórios ou ambíguos não viram elegibilidade por pontuação. Para vagas presenciais/híbridas, a localidade precisa estar ligada explicitamente ao local de trabalho; o Entorno não é incluído e Planaltina só é aceita quando identificada como DF. Compatibilidade técnica e potencial de carreira dependem de evidências na descrição. O horário não é inferido da data de publicação: quando não indicado, aparece como **“Horário não informado”**. Salário ausente aparece como **“Não divulgado”**.

Pesos e limites de prioridade estão em `sites_config.yaml`. Consultas e fontes públicas estão em `google_search_sites.yaml`. Ajuste as listas de termos de acordo com o perfil; as regras de pontuação e validação ficam em `radar_logic.py`.

## Configuração

Adicione estes secrets em **Settings → Secrets and variables → Actions**:

| Secret | Obrigatório | Descrição |
|---|---:|---|
| `SERPER_API_KEY` | Sim | Chave do Serper.dev |
| `TELEGRAM_BOT_TOKEN` | Sim | Token do bot criado com @BotFather |
| `TELEGRAM_CHAT_ID` | Sim | Chat que receberá as mensagens |

As consultas definem português (`pt-br`) e Brasil (`br`) como idioma e viés de busca. Isso orienta o Google, mas **não comprova** que a vaga seja elegível; a validação é feita separadamente pelo conteúdo do resultado.

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

O ID curto é incluído na notificação. Atualize um radar por vez (`radar_a` ou `radar_b`); registros legados com status global são migrados para os radares existentes sem sobrescrever status individuais já gravados. O histórico mantém a trilha de alterações de status separadamente para cada radar. No GitHub Actions, execute manualmente o workflow e escolha **history** para listar o histórico ou **set-status** para atualizar um status.

O histórico continua no cache gratuito do GitHub Actions, não em armazenamento durável. O cache é imutável, limitado por escopo de branch e pode expirar ou ser removido pelo GitHub; portanto, perda de cache pode causar notificações repetidas e perda do histórico/status. As chaves incluem o número da tentativa (`run_attempt`) e cada execução restaura pelo prefixo para evitar que um rerun reutilize uma chave de gravação imutável. Isso reduz colisões, mas não elimina perdas do cache nem oferece garantias de transação/backup. A opção **history** mostra o estado restaurado no Actions; a execução local mostra o `seen_jobs.json` disponível naquela cópia.

## Notificações e cadência

- Vagas de alta prioridade (80–100) são notificadas logo após a busca.
- Média (60–79) e baixa (abaixo de 60) ficam pendentes para o resumo diário.
- O resumo diário e o heartbeat são programados para **23:30 UTC (20:30 em Brasília)**. O estado persiste a data do último resumo para impedir repetição no mesmo dia; a execução manual não envia o resumo diário.
- Cada mensagem inclui radar, pontuação/prioridade, contrato, modalidade, localidade, turno, salário, tecnologias, requisitos, compatibilidade, pontos de atenção, data, status e link original.

Há seis grupos de busca por execução, com até 10 resultados por consulta. O cron roda a cada quatro horas (`30 3,7,11,15,19,23 * * *`), aproximadamente 36 créditos Serper/dia antes de execuções manuais. O resumo é enviado somente na execução de 23:30 UTC, sem uma busca adicional. O saldo gratuito do Serper é limitado; reduza grupos ou frequência se necessário. Nenhum scraping autenticado, CAPTCHA ou endpoint privado é utilizado.

## Testes

```powershell
python -m unittest -v
```

Os testes cobrem regras obrigatórias, pontuação/prioridade, horários não inferidos, migração do estado legado, deduplicação entre radares e atualização de status.
