# Auditoria — Pipoca & Play

## Diagnóstico inicial

O repositório original continha uma interface de questionário e uma função de recomendação OpenAI, mas não possuía landing page, tela de login, sessão, papéis de usuário, painel administrativo nem proteção da rota `/api/recommend`. O histórico dependia exclusivamente de `window.storage`, que não existe em navegadores comuns. A interface já tinha os sete filtros do MVP e uma experiência de resultados com três cartões.

## Correções aplicadas

A aplicação agora possui landing page pública, tela de login e cadastro individual, sessão por cookie HttpOnly assinado com HMAC, papel `admin`, painel `/api/admin/status`, logout e proteção de recomendações no backend. Cada conta de cliente possui hash PBKDF2, status `pending`, `approved` ou `rejected`, e o frontend acompanha a situação do pedido em `/api/auth/status`.

O painel administrativo passou a listar pedidos e contas, com ações protegidas para aceitar, rejeitar ou excluir usuários. A base usa JSON local em desenvolvimento e Redis REST compatível com Vercel KV/Upstash em produção serverless; sem armazenamento persistente configurado em produção, o cadastro é bloqueado com resposta controlada.

O backend agora valida o JSON da IA e exige exatamente três recomendações ordenadas. Foi criado `ContentMetadataProvider`, com adaptador TMDB opcional para poster, backdrop, duração, gêneros e disponibilidade brasileira. Sem fonte externa configurada, a interface exibe disponibilidade não confirmada em vez de atribuir fatos ao modelo.

O histórico ganhou fallback para `localStorage`, mantendo compatibilidade com o armazenamento do ambiente quando disponível. A documentação e `.env.example` foram atualizados com as variáveis de autenticação, catálogo e persistência.

## Validação visual local

A landing page foi aberta em `http://127.0.0.1:8000/` e exibiu corretamente CTA, proposta de valor e os três indicadores. O botão de entrada abriu a tela de login com abas de entrada e cadastro; a aba de cadastro exibiu e-mail, senha e confirmação de senha.

Foi criado um cadastro de teste com e-mail e senha individuais. A interface exibiu a mensagem de aguardo — “O seu acesso será liberado assim que o administrador validar. Por favor, aguarde a liberação.” — e o status ficou disponível para atualização automática.

O login local administrativo de teste `admin@test.local` / `admin-password` foi aceito e criou sessão. O painel exibiu uma pendência real com as ações “Aceitar acesso”, “Rejeitar” e “Excluir usuário”. Após aceitar o cadastro, os contadores mudaram para 0 pendências e 1 acesso liberado, e a conta do cliente entrou na lista como aprovada. O login do cliente com a senha cadastrada abriu corretamente o primeiro filtro da plataforma.

A partir do painel, o botão “Nova busca” continua abrindo o primeiro filtro e a seleção “Suspense / Thriller” avança automaticamente para o segundo filtro, mantendo os sete passos oficiais.

O fluxo autenticado também avançou corretamente pela vibe “Sombrio / Assustador” e pelo tempo “Padrão (90 a 120 min)”, confirmando a progressão visual e a separação entre clima e duração.

O wizard avançou corretamente por “Anos 2010s” e “Netflix”, confirmando também o filtro de plataforma independente dos demais.

As últimas etapas “Em Casal” e “Aclamados pela Crítica / Premiações (Oscar, Cannes)” levaram ao resumo “Sua busca”, que exibiu corretamente todos os sete valores antes da execução.

Após reiniciar com um `.env` de teste, uma sessão antiga foi invalidada e o navegador voltou à landing page. A tela de login aceitou o formulário de `cliente@pipocaplay.com`; o teste HTTP correspondente confirmou papel `user` e bloqueio de `/api/admin/status` com HTTP 403.

No navegador, o usuário comum foi redirecionado ao primeiro filtro sem botão de painel admin. O botão “Histórico” abriu o estado vazio corretamente, confirmando que a ausência de `window.storage` não quebra a página graças ao fallback para `localStorage`.

Foi simulada uma resposta JSON válida no navegador para não depender de chave OpenAI durante o teste. `runSearch` terminou em `results`, renderizou exatamente três recomendações, match, ratings, Oscar, links de verificação, fallback visual e aviso de disponibilidade não confirmada; também salvou um item no histórico via `localStorage`.

## Correções de persistência e acesso administrativo

O cadastro em produção falhava com “Não foi possível gravar a base de contas. Configure um armazenamento persistente.” porque o deploy não tinha nenhum armazenamento conectado e o código caía no arquivo local, que o filesystem somente leitura das funções serverless recusa.

A camada de armazenamento passou a detectar quatro backends, nesta ordem: `postgres` (`POSTGRES_URL`/`DATABASE_URL`), `vercel-blob` (`BLOB_READ_WRITE_TOKEN`), `redis-rest` (`KV_REST_API_URL`) e `arquivo-local`. O Postgres é a opção recomendada, cria a tabela `pipoca_play_store` sozinha na primeira gravação e usa `psycopg[binary]`, já declarado em `requirements.txt`. A gravação local agora recusa explicitamente o modo serverless e a mensagem de erro passou a nomear o passo a passo real na Vercel, em vez de pedir genericamente “um armazenamento persistente”. Foi adicionado `GET /api/health`, público e sem segredos, que informa qual backend está ativo, se ele é persistente e se as credenciais de administrador existem.

O acesso administrativo ganhou página dedicada em `/admin`, com login próprio, contadores, busca, filtro por situação e as ações de liberar, rejeitar, voltar para pendente, redefinir senha, promover, rebaixar, excluir e criar acesso já liberado. Além do administrador raiz definido por `ADMIN_EMAIL`/`ADMIN_PASSWORD`, contas gravadas na base podem receber o papel `admin` e entrar pelo mesmo painel. O administrador logado é impedido, no backend e na interface, de retirar o próprio acesso. O painel exibe um bloco de diagnóstico com o backend em uso e as variáveis encontradas, e mostra o passo a passo da Vercel quando nenhum banco está conectado.

As rotas de autenticação e administração passaram a compartilhar `api_core.py`, chamado tanto por `server.py` quanto pelas funções em `api/`, eliminando a duplicação que fazia o servidor local e o runtime serverless divergirem. A suíte `test_app.py` subiu de 11 para 27 testes, cobrindo o backend Postgres com driver falso, a recusa de gravação em modo serverless, o ciclo completo de ações administrativas, a proteção da própria conta, o payload de `/api/health` e as respostas de login.

## Rodada 2 — assinatura, créditos, marcações e checkout

### Diagnóstico

O deploy publicado (commit `919e061`) tinha landing page, cadastro com aprovação, painel `/admin` e o motor de sete filtros, mas **nenhuma** das funcionalidades de monetização e personalização pedidas: não havia catálogo de planos, contagem de créditos, marcações do usuário, checkout, integração com a Asaas, controle do botão voltar, encerramento de sessão ao sair da página nem exibição da senha digitada. O código também não continha qualquer referência à Asaas — a integração precisou ser escrita do zero.

### Correções aplicadas

Foram criados `plans.py` (catálogo Silver/Gold/Diamante), `billing.py` (cliente da API Asaas, validação de CPF/CNPJ, assinatura mensal, URL de fatura e leitura do webhook) e `recommender.py` (motor extraído de `server.py`, agora compartilhado sem divergência entre o servidor local e as funções serverless). O `user_store.py` ganhou assinatura, ciclo de 30 dias, créditos diários no fuso de Brasília e as marcações por título; o `api_core.py` ganhou as rotas de conta, planos, marcações, checkout, conferência de pagamento, webhook e a rota de recomendação com débito de crédito e devolução em caso de falha do motor.

Na interface, o cliente sem assinatura cai na vitrine de planos, paga pela Asaas e só recebe indicações após a confirmação; cada card traz *gostei*, *não gostei* e *já assisti*, gravados na conta e injetados no prompt; a tela **Marcações** permite remover item por item; a navegação usa a History API para o botão voltar nativo; a sessão virou cookie de sessão do navegador com logout no `pagehide`; e os campos de senha ganharam o ícone de olho.

### Validação

`python3 -m unittest test_app` — 51 testes, todos verdes, incluindo consumo e reposição de créditos por plano, bloqueio sem pagamento, webhook com e sem token válido, suspensão por inadimplência e remoção individual de marcações.

Fluxo HTTP completo executado contra o servidor real com Asaas e OpenAI simulados: cadastro → aprovação pelo admin → login → bloqueio sem plano → checkout → webhook de confirmação → duas consultas do plano Silver → bloqueio por falta de crédito → marcação enviada ao prompt → remoção da marcação → sessão encerrada ao sair.

Interface validada em Chromium (390×844, sem erros de JavaScript no console): olho da senha, vitrine com os três preços, checkout com recusa de CPF inválido, espera pela confirmação, liberação após o webhook, contador de créditos no topo, marcação nos cards, histórico de marcações, aviso de limite diário, remoção individual e retorno pelo botão voltar. Recarregar a página desconecta o cliente e exige novo login, como especificado.

## Rodada 3 — tipo de produção (filme, série ou mescla)

Foi acrescentada a **primeira pergunta do questionário**: *O que você quer ver hoje?* — **Filme**, **Série** ou **Mesclar (filmes e séries)** —, passando o fluxo de sete para oito filtros. O tipo escolhido é a restrição mais forte do prompt: em "Mesclar", a lista traz pelo menos um filme e pelo menos uma série.

O schema da resposta passou a exigir `content_type` (`filme`/`serie`) e `seasons` em cada indicação; a validação normaliza um `content_type` ausente para `filme`. O card mostra o selo do formato e, em séries, o número de temporadas e a duração média por episódio. O `metadata.py` passou a consultar `/search/tv` e `/tv/{id}` para séries — buscar série na rota de filmes traria o pôster errado — e a busca de pôster na Wikipedia usa o sufixo `(TV series)`. O construtor de prompt duplicado que existia no navegador, sem nenhuma chamada, foi removido para não divergir do prompt real do backend.

Validação: 56 testes automatizados verdes, incluindo as três respostas oficiais da nova pergunta, a recusa de resposta fora do catálogo, o texto do prompt em "Mesclar", o schema e a separação dos endpoints de série e filme no TMDB. O fluxo HTTP completo e as 31 verificações de interface em Chromium foram repetidos com as oito perguntas, com os cards exibindo corretamente `FILME`, `SÉRIE` e as temporadas.

## Rodada 4 — build da Vercel quebrado pelo limite de funções

O deploy de preview do PR falhou. A causa: cada arquivo em `api/` vira uma função serverless, e o plano Hobby da Vercel aceita no máximo 12 por deploy. A `main` tinha 9 funções e publicava normalmente; as seis rotas novas (planos, conta, marcações e as três de cobrança) levaram o total a 15, e o build passou a ser recusado antes de qualquer código rodar.

A correção foi concentrar todo o `/api` em **uma única função**: `router.py` resolve método e caminho e devolve `(status, payload, headers)`; `api/index.py` é o invólucro HTTP dessa função na Vercel, alcançado pela reescrita `/api/:path*` → `/api/index` no `vercel.json`; e o `server.py` passou a usar o mesmo roteador, de modo que produção e desenvolvimento compartilham a resolução de rotas. O deploy foi de 15 para 1 função. O `maxDuration` subiu de 15s para 60s, alinhando o limite da função ao timeout de 45s da chamada à OpenAI — antes, uma resposta lenta era cortada pela plataforma.

Um 404 de roteamento passou a devolver o caminho recebido, o que torna imediato o diagnóstico caso alguma reescrita altere a rota em produção.

Validação: 62 testes automatizados, incluindo uma bateria que exige que cada rota publicada exista no roteador, a recusa de método errado e a checagem de que o deploy tem uma única função. O fluxo HTTP completo foi executado duas vezes — pelo servidor local e **pela própria função serverless de `api/index.py`**, servida com o mesmo handler que a Vercel usa — e as 31 verificações de interface em Chromium foram repetidas, todas verdes.

## Rodada 5 — marcação múltipla de streamings, avaliações ampliadas, preços e contato

### Onde pretende assistir: várias plataformas de uma vez

A sexta pergunta deixou de ser de resposta única. O cliente marca **quantos serviços quiser**, até cinco por busca, e cada opção mostra a **logo do serviço** — selos SVG desenhados no próprio arquivo, sem requisição externa, para que a etapa nunca fique sem imagem. Marcar *Livre (Qualquer)* limpa as demais marcações, porque a opção abre o catálogo inteiro e anularia o filtro se convivesse com um serviço específico. Atingido o teto, a tela avisa em vez de trocar a seleção por baixo do usuário.

O contrato do backend acompanhou a mudança em vez de confiar na tela. O `clean_filters` ganhou `MULTI_ANSWER_KEYS` e passou a aceitar lista **ou** texto na chave `platform`, validando cada serviço contra o catálogo oficial, removendo duplicatas e aplicando o mesmo teto de cinco. A divisão é feita só nas perguntas multivaloradas: opções de resposta única como *Aclamados pela Crítica / Premiações (Oscar, Cannes)* já trazem vírgula no próprio nome e seriam quebradas por um split cego.

O prompt passou a receber a lista inteira por meio de `build_platform_section`, com três redações distintas: sem restrição, um único serviço, ou o conjunto marcado — neste caso exigindo que cada indicação esteja em pelo menos uma das plataformas marcadas, que nenhuma dependa de serviço fora da lista e que as três opções sejam distribuídas entre elas quando houver bons títulos em mais de uma.

### Avaliações de nove fontes

O bloco `ratings` do schema saiu de duas fontes (IMDb e Rotten Tomatoes) para nove: **IMDb, Rotten Tomatoes crítica, Rotten Tomatoes público, Metacritic, Google, TMDB, Letterboxd, AdoroCinema e Mercado Livre Filmes**. Schema, prompt e tela derivam todos do mesmo catálogo `RATING_SOURCES`, cada fonte com a sua escala — o modo estrito da API exige toda propriedade em `required`, então a fonte desconhecida volta como `0` e simplesmente não vira pílula na tela, nunca uma nota estimada.

O enriquecimento externo passou a preencher a nota do TMDB a partir do próprio TMDB, e um valor vindo da fonte externa **sobrescreve** o que o modelo tiver lembrado. Os links de "conferir na fonte" cobrem agora as mesmas nove fontes.

Na tela, nove pílulas em coluna única deixariam o ingresso 298px mais alto, e comprimi-las em uma linha truncava "Mercado Livre" e "Tomatômetro". A solução foi uma grade que se adapta à largura do ingresso, com o nome da fonte acima e a nota abaixo: nenhuma abreviação, cinco linhas em vez de nove.

### Preços, contato e sigilo do motor

Os planos foram reprecificados: **Silver R$ 10,00, Gold R$ 15,00 e Diamante R$ 20,00**. O `plans.py` é a única origem desses valores — vitrine, checkout e cobrança leem de lá.

Um **botão flutuante de WhatsApp** aponta para o número (11) 93425-2085. Ele fica fora de `#app`, porque cada `render()` esvazia o container, e o rodapé da página ganhou folga para o botão não cobrir conteúdo.

A tela de resultado deixou de estampar `MOTOR: CHATGPT`. Junto com o rótulo, foram neutralizadas as mensagens de erro que chegavam ao navegador nomeando o provedor — chave ausente, falha HTTP, falha de conexão, resposta vazia e JSON inválido —, todas reescritas como "motor de recomendação". O detalhe técnico continua no encadeamento da exceção, para os logs do servidor.

### Validação

`python3 -m unittest test_app` — **81 testes verdes**, entre eles: marcação múltipla vinda como lista e como texto, remoção de duplicatas e vazios, precedência de *Livre (Qualquer)*, recusa de serviço inventado, teto de cinco serviços, as três redações do prompt de plataforma, o catálogo de avaliações governando o schema, a nota externa sobrescrevendo a do modelo, os três preços novos e a garantia de que nenhuma mensagem de erro entregue ao cliente nomeia o provedor do motor.

Interface exercitada em Chromium (430×900, sem erro de JavaScript vindo da aplicação): marcação de três serviços com as logos, aviso ao tentar o sexto, *Livre (Qualquer)* limpando as demais, resumo exibindo "Netflix, Disney+", as nove pílulas de avaliação sem truncamento, os oito links de conferência, o subtítulo do resultado sem qualquer menção ao motor e o botão do WhatsApp visível e apontando para `wa.me/5511934252085`.

## Rodada 6 — histórico por conta e sinopse/premiações somem do card

### Diagnóstico

O deploy publicado trocou `public/app.html` e `public/index.html` pelo layout gerado no Claude Web Design (commit `d3f84c9`), sem repassar todo o comportamento anterior. Dois problemas vieram dessa troca:

O **histórico de buscas** continuava gravado só em `window.localStorage`, com a mesma convenção de chave da versão anterior — cada navegador/aparelho enxergava uma lista diferente, mesmo com a mesma conta logada em ambos.

O card de resultado passou a exibir `why_it_matches` (o "por que combina") mas **descartava silenciosamente** `synopsis` e o bloco `awards` que o backend já preenchia (o schema de `recommender.py` sempre exige os dois, com ou sem `TMDB_API_KEY`) — a tela simplesmente não tinha marcação para eles. O pôster depende de `TMDB_API_KEY`; sem essa variável configurada na Vercel, `metadata.py` devolve `poster_url` vazio de propósito (para não inventar uma imagem), e o card cai no gradiente de fallback.

### Correções aplicadas

`user_store.py` ganhou `add_history`/`list_history`/`remove_history`, no mesmo formato de `feedback`: histórico por `user_id`, até 50 buscas, removível uma a uma. `api_core.recommend` grava a busca na conta assim que a recomendação é validada — antes de qualquer chamada do navegador — e `router.py` publica `GET/POST/DELETE /api/history`. `public/app.html` (o `/app`) passou a carregar o histórico de `/api/history` ao entrar e depois de cada busca, com o `localStorage` mantido só como modo de demonstração (sessão inalcançável).

Os cards de resultado (tela de busca e histórico) ganharam um parágrafo de **Sinopse** (só quando diferente do texto de "por que combina", para não duplicar) e uma linha de **premiações** com o destaque e a contagem de Oscars, quando o backend os preenche.

### Validação

`python3 -m unittest test_app` — 83 testes verdes, incluindo a gravação da busca na conta ao chamar `/api/recommend`, a leitura por uma segunda "sessão" da mesma conta, a remoção individual e o bloqueio de `/api/history` sem login.

Fluxo completo em Chromium (Playwright), servidor local com um motor de recomendação simulado: login, oito perguntas, card exibindo sinopse e o destaque de premiação, aba Histórico mostrando a busca; um **segundo contexto de navegador**, com `localStorage` vazio e a mesma sessão, abriu a aba Histórico e viu a mesma busca — confirmando que a lista agora vem da conta, não do navegador. Apagar pelo card removeu o item também via `/api/history`. Sem console de erros JavaScript em nenhuma etapa.

Pôster: a amarração `poster_url → background-image` foi confirmada (o estilo chega ao DOM com a URL do TMDB); a imagem não teve como carregar no sandbox de teste, sem saída de rede — isso é esperado ali e não indica um problema de código. Em produção, sem imagem no pôster o motivo mais provável é `TMDB_API_KEY` ausente nas variáveis de ambiente da Vercel.

## Rodada 7 — pôster também sai da própria chamada do motor (ChatGPT)

### Diagnóstico

O pedido foi explícito: o pôster não deveria depender de `TMDB_API_KEY` — deveria vir da própria chamada da API do ChatGPT. Antes desta rodada, `poster_url` só existia como campo *derivado* em `metadata.py`; o schema de `recommender.py` nunca pedia ao modelo uma URL de imagem, então sem TMDB configurado o pôster ficava sempre vazio, por desenho.

O risco de pedir isso ao modelo é conhecido: uma LLM de texto pode "lembrar" uma URL de imagem que parece plausível mas não existe ou não carrega — diferente de avaliações e disponibilidade, aqui o preço de errar é só visual (um pôster que não aparece), não um dado incorreto exibido como fato.

### Correções aplicadas

`recommender.py` passou a exigir `poster_url` no schema de cada indicação (`RECOMMENDATION_SCHEMA`) e o prompt ganhou uma instrução dedicada (`_poster_prompt_line`): preencher com um link direto e público (`.jpg`/`.jpeg`/`.png`/`.webp`) só quando o modelo tiver certeza de que é real, e deixar vazio caso contrário — a proibição geral de "não invente URLs" foi restrita ao pôster, com a mesma exigência de honestidade.

`metadata.py` (`enrich_result`) continua preferindo o TMDB quando `TMDB_API_KEY` está configurada e o título é encontrado (`poster_source: "tmdb"`); sem isso, usa o `poster_url` que o próprio motor indicou, com uma checagem de forma mínima (`_clean_model_poster_url`: precisa começar com `http(s)://` e terminar em extensão de imagem) e marca `poster_source: "model"`.

Como o modelo pode errar mesmo com um link de forma válida, o front-end (`public/app.html`) nunca exibe um pôster de fonte `"model"` direto: `verifyPoster` carrega a imagem de verdade num `Image()` do navegador antes de confiar nela (`trustedPosterUrl`), e só troca o gradiente pela imagem depois que ela carregar — nunca aparece um ícone de imagem quebrada. Pôster de fonte `"tmdb"` (já confirmado por uma fonte externa) continua sendo exibido direto, sem essa espera.

### Validação

`python3 -m unittest test_app` — 86 testes verdes, incluindo o pôster do motor sendo aceito sem TMDB, um valor sem forma de URL de imagem sendo descartado, e o TMDB sobrescrevendo o pôster do motor quando encontra o título.

Em Chromium (Playwright), com uma resposta do motor simulada contendo `poster_url` e sem `TMDB_API_KEY`: confirmado via `/api/history` que a indicação salva carrega `poster_source: "model"`; com o link do pôster interceptado e respondido com uma imagem real, o card passou a exibi-lo após o carregamento (antes e depois checados via `background-image` no DOM); sem essa interceptação (rede real bloqueada no sandbox), o card permaneceu no gradiente em vez de mostrar uma imagem quebrada.

## Rodada 8 — remove o TMDB; pôster nunca mais fica vazio

### Diagnóstico

O pedido foi direto: o projeto não terá `TMDB_API_KEY` disponível, e o pôster deve vir **unicamente** da própria chamada do motor — nunca vazio, gerando uma imagem ou buscando na internet quando necessário. Uma busca real por imagem no Google exigiria uma chave própria da API do Google (Custom Search), o que reintroduziria exatamente o problema que o pedido queria evitar (uma chave que o projeto não vai ter). A alternativa combinada com o usuário: tentar uma imagem real e gratuita na Wikipedia (API pública, sem chave) antes de gerar qualquer coisa por IA, e só gerar como último recurso — sempre rotulado como capa ilustrativa, nunca como o pôster oficial.

### Correções aplicadas

`TMDBMetadataProvider`, `NullMetadataProvider`, `ContentMetadataProvider` e `get_metadata_provider` foram removidos de `metadata.py` — não há mais nenhum catálogo externo nem chave associada. `TMDB_API_KEY` saiu do `.env.example`, do README e de qualquer variável lida pelo backend.

`metadata.py` ganhou `resolve_poster`, chamada por `enrich_result` para cada indicação, nesta ordem:

1. **O link do próprio motor** (`poster_url` do schema, já pedido desde a Rodada 7), validado só na forma.
2. **Wikipedia/Wikimedia** (`_wikipedia_poster` → API REST pública `page/summary`, sem chave): tenta o título original e o em português, com sufixos de desambiguação (`(TV series)`, `(<ano> film)`, `(film)`), e descarta páginas de desambiguação.
3. **Geração por IA** (`_generate_poster_image`, último recurso): chama `POST /v1/images/generations` da própria conta OpenAI já usada pelo motor (mesma `OPENAI_API_KEY`, nenhuma chave nova) pedindo uma arte de capa sem texto, título ou logotipo, e sem representar atores reais; devolve uma `data:image/png;base64,...` embutida na própria resposta.

`poster_source` acompanha a origem (`"model"`, `"wikipedia"`, `"generated"` ou `""` só se as três tentativas falharem — o que só acontece sem `OPENAI_API_KEY`, que já é obrigatória para a plataforma existir). Como a disponibilidade em streaming também dependia do TMDB para ser "confirmada", `where_to_watch` deixou de ser sempre zerado sem uma fonte externa: agora mantém o que o próprio motor respondeu (já era isso que o prompt pedia), só marcado como não confirmado.

No front-end (`public/app.html`), pôster de fonte `"wikipedia"` ou `"generated"` é exibido direto (o backend já confirmou a imagem antes de devolver); só `"model"` continua passando pela validação assíncrona no navegador (`verifyPoster`/`trustedPosterUrl`) antes de aparecer, porque é a única fonte que o próprio motor pode ter errado sem qualquer confirmação. Uma capa gerada por IA ganhou um selo "Capa ilustrativa (gerada por IA)" sobre o pôster, para nunca ser confundida com a arte oficial.

### Validação

`python3 -m unittest test_app` — 87 testes verdes: a cadeia completa (motor → Wikipedia → geração) testada isoladamente em cada camada, incluindo a chamada real ao endpoint de geração de imagem com `urlopen` simulado, a leitura do resumo da Wikipedia e o descarte de páginas de desambiguação, e a garantia de que o pôster só fica vazio se as três fontes falharem.

Fluxo completo em Chromium (Playwright) contra o servidor local, com `_wikipedia_poster` e `_generate_poster_image` simulados (a rede real para Wikipedia e OpenAI está bloqueada neste ambiente de sandbox): a indicação sem link do motor recebeu o pôster da Wikipedia e a indicação sem nenhuma das duas recebeu a capa gerada (uma imagem real embutida em base64, que carregou de verdade no navegador) — ambas exibidas **imediatamente**, sem a espera de validação que só o pôster de fonte `"model"` tem; o selo "Capa ilustrativa" apareceu somente sobre a capa gerada; e `/api/history` confirmou `poster_source` correto (`"model"`, `"wikipedia"`, `"generated"`) para cada uma das três indicações.

## Rodada 9 — a imagem coletada da resposta do motor chega ao cartão

### Diagnóstico

O pedido: o prompt enviado ao motor já pede o endereço da imagem do pôster, então esse dado precisa ser **coletado e inserido no output** do usuário. Analisando a cadeia da Rodada 8, três pontos da coleta impediam exatamente isso:

1. **O link do motor vencia sem nenhuma confirmação.** `resolve_poster` aceitava o `poster_url` só pela forma (começar com `http(s)://` e terminar em extensão de imagem) e parava ali. Uma LLM erra endereços de imagem com facilidade — e, quando errava, a coleta já tinha terminado: as duas fontes que funcionam (Wikipedia e geração) nunca eram tentadas, e o cartão ficava no gradiente. O front-end até testava a imagem no navegador (`verifyPoster`), mas ao falhar não tinha para onde voltar.
2. **`http://` era aceito como está.** O deploy é servido por HTTPS: um pôster em `http://` é bloqueado pelo navegador como conteúdo misto. O backend dava o link por bom, marcava `poster_source: "model"` e a imagem nunca aparecia — de novo sem cair para a próxima fonte.
3. **A coleta podia estourar o tempo da função.** As três indicações eram resolvidas em sequência, cada uma com até seis idas à Wikipedia (6s cada) e uma geração de imagem de até 55s, dentro de uma função com `maxDuration` de 60s que já tinha gasto até 45s falando com o motor. No pior caso a recomendação inteira — já paga com um crédito — morria por timeout.

O prompt também pedia silêncio na dúvida ("caso contrário, deixe `poster_url` como string vazia"), o que fazia o modelo devolver campo vazio justamente na única fonte que conhece o pôster do título.

### Correções aplicadas

`metadata.py` passou a tratar a resposta do motor como o ponto de coleta principal:

- `_looks_like_image_url` normaliza antes de julgar: promove `http://` para `https://`, aceita também endereços sem extensão quando o hospedeiro só serve imagem (`tmdb.org`, `wikimedia.org`, `media-amazon.com` e afins) e descarta endereços internos (`localhost`, faixas privadas) — a URL vem do motor, não de uma fonte confiável.
- `_image_responds` confirma o link **no servidor** antes de aceitá-lo: um `GET` com `Range: bytes=0-0`, que olha o status e o `Content-Type` sem baixar a imagem nem repassar o corpo. Só um "sim" faz o link virar pôster; um "não" devolve a coleta para a fila (Wikipedia → geração), que era o passo que faltava.
- `_wikipedia_poster` passou a consultar a Wikipedia **em português antes da inglesa** (`(filme de <ano>)`, `(filme)`, `(série de televisão)`), onde costuma estar a capa do lançamento nacional, com a lista de tentativas limitada por `MAX_WIKIPEDIA_CANDIDATES`.
- `enrich_result` aceita um `deadline` e resolve as três indicações **em paralelo** (`ThreadPoolExecutor`), com cada etapa encurtando o próprio tempo limite pelo que sobrou do prazo. `recommender.call_openai` marca o relógio antes de chamar o motor e passa `started + REQUEST_BUDGET_SECONDS` (52s, abaixo do `maxDuration` de 60s do `vercel.json`), então a busca da imagem usa o tempo que sobrou e nunca derruba a recomendação. Um pôster que falha é isolado (`_safe_resolve_poster`) e não leva a resposta junto.

`recommender._poster_prompt_line` deixou de pedir silêncio na dúvida: como o servidor confere o endereço e troca de fonte sozinho quando ele não responde, o prompt agora pede o melhor link que o modelo conhecer, sempre em `https`, e explica que precisa ser o arquivo da imagem — nunca uma página HTML, um resultado de busca ou o endereço da página do filme.

No front-end (`public/app.html`), `verifyPoster`/`_posterCache` saíram: como o backend confirma a imagem antes de devolvê-la, `trustedPosterUrl` lê o campo direto e o pôster de fonte `"model"` aparece **imediatamente**, sem a espera que antes deixava o cartão no gradiente. O gradiente do cartão continua atrás da imagem, então nada quebra se ela ainda assim não carregar.

### Validação

`python3 -m unittest test_app` — 100 testes verdes (13 novos), entre eles: o link do motor confirmado virando `poster_source: "model"`; o link com forma boa mas sem resposta **cedendo lugar à Wikipedia** (o caso que antes deixava o cartão vazio); `http://` chegando ao output como `https://`; endereço de CDN de imagem sem extensão aceito e endereço interno recusado; `data:` embutido pulando a checagem de rede; a sonda aceitando só resposta `200/206` com `Content-Type: image/*` e pedindo um único byte; a Wikipedia sendo consultada em português antes da inglesa; a coleta parando sem abrir conexão quando o prazo já passou; as três indicações resolvidas ao mesmo tempo; e uma falha de pôster não derrubando a recomendação.

De ponta a ponta contra o servidor local, com o motor e a rede de imagens simulados (a rede real está bloqueada neste sandbox): as três indicações saíram com imagem — a do link bom como `model`, a do link morto e a sem link como `wikipedia` — e o mesmo resultado chegou por HTTP em `/api/recommend`.

Em Chromium (Playwright), logado e percorrendo as oito perguntas até o Top 3: os três cartões pintaram `background-image` (`url("https://cdn.exemplo.test/posters/bom.jpg")` no primeiro, a capa da Wikipedia nos outros dois) e o navegador buscou de fato as duas imagens — o pôster vindo da resposta do motor apareceu direto, sem a espera de validação da rodada anterior.

## Rodada 10 — o cartão passa a trazer a arte oficial do título

### Diagnóstico

O relato veio com um PDF da tela publicada: os três cartões traziam o selo **"Capa ilustrativa (gerada por IA)"** e o quadro do pôster vazio. O selo é a prova do que aconteceu — ele só aparece quando `poster_source === "generated"` **e** `poster_url` não está vazio. Ou seja, a cadeia caiu até o último recurso nas três indicações, e a capa gerada ainda por cima não pintou.

Os títulos do PDF (*De Volta à Ação*, *Um Tira da Pesada 4: Axel Foley*, *Lift: Roubo nas Alturas*) são três lançamentos grandes da Netflix, todos com verbete na Wikipedia e arte publicada em loja. Nenhuma fonte de arte real acertou, por três motivos somados:

1. **O motor devolvia `poster_url` vazio.** Até a Rodada 9 o prompt mandava deixar o campo vazio na dúvida ("Só preencha quando tiver certeza… caso contrário, deixe poster_url como string vazia"), e é exatamente isso que um modelo honesto faz com endereços de imagem. A única fonte que conhecia o título calava.
2. **A Wikipedia era consultada adivinhando o título exato do verbete** (`page/summary` de "Fulano (filme de 2024)", "Fulano (film)", "Fulano"). Verbete real com outro nome — que é a regra, não a exceção — dava 404 e a coleta seguia.
3. **Não havia nenhuma fonte de arte oficial de verdade**, só a Wikipedia. Sem ela, sobrava a capa gerada — que não atende o pedido de todo modo: o usuário quer **a imagem do filme**, não uma ilustração inspirada nele.

E a capa gerada, além de não ser o que se pede, chegava como uma `data:image/png;base64,…` de vários MB embutida na resposta: pesada para trafegar, e o tipo declarado (`image/png`) era um chute sobre os bytes recebidos, sem nenhuma conferência.

### Correções aplicadas

`metadata.py` ganhou uma fonte de arte oficial de verdade e passou a achar o verbete por busca:

- **Busca pública do iTunes** (`itunes.apple.com/search`, sem chave), 2ª na fila: consulta a loja **brasileira** com o título em português e depois a americana com o original; `media=movie&entity=movie` para filme e `media=tvShow&entity=tvSeason` para série. É a fonte com melhor cobertura de arte oficial. A miniatura de 100px que a busca devolve é reescrita para `600x900bb.jpg` — como a reescrita é nossa, ela é confirmada, e sem resposta vale a miniatura original, que veio da própria API.
- **Wikipedia por busca** (`action=query&generator=search&prop=pageimages`), 3ª na fila, em português e depois em inglês: acha o verbete pelo nome em vez de adivinhar o título exato, que era como as tentativas anteriores erravam.
- **Conferência de título e ano** em ambas (`_titles_match`, `_entry_matches`): com o ano conhecido ele é obrigatório, senão *Um Tira da Pesada 4* casaria com o original de 1984. A comparação ignora acento, pontuação e caixa, aceita subtítulo a mais e — só junto do ano — a numeração da sequência, porque as lojas publicam "Um Tira da Pesada: Axel Foley" onde o motor diz "Um Tira da Pesada 4: Axel Foley". Sem correspondência, a fonte devolve vazio: **arte do filme errado é pior que cartão sem arte**.
- **Capa gerada como último recurso, agora comprimida** (`output_format: jpeg`, `output_compression: 60`) e com o tipo lido dos **bytes recebidos** (`_data_uri`), não do formato pedido. Uma capa acima de `MAX_DATA_URI_LENGTH` é descartada em vez de arriscar estourar o limite de corpo da função.
- **Logs** (`_log`, silenciados em teste) dizem no painel da Vercel por que um cartão caiu na capa ilustrativa — era o dado que faltava para diagnosticar isto sem adivinhação.

A ordem final da coleta é: motor → iTunes → Wikipedia → capa gerada, cada endereço confirmado antes de virar pôster, as três indicações em paralelo sob o prazo comum da Rodada 9.

### Validação

`python3 -m unittest test_app` — 107 testes verdes (7 novos nesta rodada). O principal é a **regressão do relato**: os três títulos exatos do PDF passam pela cadeia inteira, com só o transporte HTTP simulado (nenhuma função do módulo é mockada) e as respostas no formato real das APIs — os três saem com arte oficial (`itunes`, `itunes`, `wikipedia`), a capa gerada nunca é chamada, a miniatura vira `600x900bb.jpg`, e o quarto *Tira da Pesada* traz a arte de 2024, não a de 1984. Também cobertos: o verbete de outro título sendo recusado, a busca do iTunes pedindo temporada quando é série, a miniatura valendo quando o tamanho ampliado não responde, e a capa gerada declarando o tipo real dos bytes e tendo teto de tamanho.

Fluxo completo em Chromium (Playwright) contra o servidor local, com o motor e as APIs de pôster simuladas no transporte: `/api/recommend` devolveu `itunes`, `itunes` e `wikipedia`; os três cartões pintaram `background-image` com esses endereços, o navegador buscou de fato as três imagens, e o selo "Capa ilustrativa (gerada por IA)" **não apareceu em nenhum** — porque nenhum caiu na capa gerada.

Uma ressalva honesta: a rede externa está bloqueada neste ambiente, então iTunes e Wikipedia foram exercitados contra o formato real de resposta, não contra os servidores reais. O comportamento em produção depende de essas duas APIs responderem do runtime da Vercel; os logs `[poster]` foram acrescentados exatamente para que isso apareça no painel caso não respondam.

## Rodada 11 — o cadastro deixa de esperar aprovação manual

### Diagnóstico

A fila de aprovação vinha da versão anterior do produto, quando não havia pagamento: o cadastro nascia `pending`, o administrador liberava conta por conta em `/admin` e só então o cliente conseguia entrar. Com o checkout da Asaas (Rodada 2) o pagamento passou a ser o portão real da geração de resultados, e a aprovação virou uma etapa a mais sem função — o cliente cadastrava, via "Aguardando liberação" e parava ali.

O mapeamento da regra antiga, antes de qualquer alteração, encontrou dez pontos:

1. `user_store.register_user` gravava `status: "pending"` e um `status_token_hash` para o cliente acompanhar o pedido;
2. `user_store.update_user_status` / `VALID_STATUSES` / `get_status_by_token` existiam só para aprovar, rejeitar e consultar;
3. `auth.authenticate` exigia `status == "approved"` para autenticar, e `auth.read_session` derrubava a sessão via `is_approved_user`;
4. `api_core.login` devolvia 403 com "aguardando a validação do administrador" e 403 para rejeitados;
5. `api_core.register` respondia com a mensagem de espera, sem sessão;
6. `api_core.admin_action` tinha as ações `approve`, `reject` e `pending`;
7. `GET /api/auth/status` (em `router.py`) servia só para o cliente acompanhar o pedido;
8. `auth.config_status` publicava os contadores `pending_user_count` / `rejected_user_count`;
9. `public/admin.html` tinha coluna "Situação", filtro por situação, contadores de pendentes/liberados/rejeitados e os botões **Liberar**, **Rejeitar** e **Voltar p/ pendente**;
10. `public/index.html` tinha a tela "Aguardando liberação" e os textos que prometiam a validação do administrador.

O que **não** pertencia à regra antiga e por isso foi preservado: `delete`, `promote`, `demote`, `set_password`, `create`, `grant_plan` e `revoke_plan` no painel; toda a cobrança (`billing.py`, webhook, `consume_credit`, `SubscriptionRequired`); e os estados de *assinatura* `none`/`pending`/`active`/`past_due`/`canceled`, que são do pagamento e apenas repetem a palavra "pending".

### Correções aplicadas

O campo `status` do cadastro deixou de existir: a conta nasce ativa e o acesso ao resultado continua valendo pelo pagamento.

- **`user_store.py`** — `register_user` cria a conta já ativa e devolve só o registro (o e-mail repetido segue recusado, agora sem exceção para rejeitados, o que impede sobrescrever a senha de uma conta existente); `create_user` perdeu o parâmetro `status`; `update_user_status`, `get_status_by_token` e `VALID_STATUSES` saíram; `is_approved_user` virou `user_exists` (a sessão morre quando a conta é excluída); `is_admin_user` e `update_user_role` passaram a olhar só o papel; `admin_users` ordena por data de cadastro; `admin_summary` conta `total`, `admins` e `subscribers`.
- **`auth.py`** — `authenticate` não consulta mais o status; `read_session` usa `user_exists`; `config_status` troca os contadores de pendentes/rejeitados por `user_count` e `subscriber_count`.
- **`api_core.py`** — `register` devolve **201 com o `Set-Cookie` da sessão** e a mensagem que aponta o próximo passo (escolher plano e pagar); sem `AUTH_SECRET` a conta é criada e a resposta vem com `authenticated: false`, sem sessão inválida. `login` perdeu os dois 403; `registration_status` saiu; `admin_action` perdeu `approve`/`reject`/`pending` e `SELF_DESTRUCTIVE_ACTIONS` ficou em `{delete, demote}`.
- **`router.py`** — a rota `GET /api/auth/status` saiu (e com ela o helper `_first`, que só existia para ela); `register` recebe `is_secure_request(headers)` para marcar o cookie como `Secure`.
- **`public/index.html`** — a tela "Aguardando liberação" foi removida com o estado `authSent` e o `backToLogin`; o cadastro termina como o login, indo direto para `/app`; os textos que falavam em análise do administrador viraram a jornada real ("o acesso é criado na hora: em seguida você escolhe o plano e conclui o pagamento") e o botão passou a ser "Criar acesso e continuar".
- **`public/admin.html`** — saíram a coluna "Situação", o filtro por situação, os contadores de pendentes/liberados/rejeitados e os três botões da aprovação; ficaram **Nova senha**, **Liberar plano**, **Cancelar plano**, **Tornar admin/cliente** e **Excluir**. As classes de cor `.tag.pending/.approved/.rejected`, que a coluna Plano também usava, viraram `.tag.warn/.ok/.off` — o rótulo "Aguardando pagamento" continua igual, porque é da assinatura.

Contas antigas gravadas como `pending` ou `rejected` continuam válidas: como ninguém mais lê o campo, elas passam a entrar normalmente e o painel as lista sem situação. Nenhuma migração de banco é necessária — a base é um documento JSON, e os campos herdados são simplesmente ignorados.

### Validação

`python3 -m unittest test_app` — 110 testes verdes. O novo `test_journey_goes_from_signup_straight_to_payment_and_result` percorre a jornada inteira pelo roteador real: cadastro devolvendo sessão → `/api/recommend` em **402** sem pagamento (e o motor não é sequer chamado) → checkout aberto ainda em 402 → webhook `PAYMENT_CONFIRMED` → `/api/recommend` em **200** gastando um crédito → painel restrito ao administrador. Também cobertos: cadastro autenticando na hora, e-mail repetido em 409, `GET /api/auth/status` respondendo 404, `approve` recusada com 400, e a sessão caindo quando a conta é excluída.

De ponta a ponta contra o servidor local: cadastro pela API devolveu `201` com `Set-Cookie`; `/api/recommend` sem plano veio `402 subscription_required`; com plano liberado pelo painel a chamada passou do portão e falhou só no motor (`502`, sem chave da OpenAI neste sandbox), com o crédito devolvido; `revoke_plan` derrubou o acesso de volta para `402`; `delete` encerrou a sessão do cliente. Uma base simulando o formato antigo (contas `pending`, `rejected` e `approved`) autenticou as três normalmente.

Em Chromium (Playwright): na landing, o cadastro leva **direto para `/app`** — a tela "Aguardando liberação" não aparece mais e nenhum texto cita validação do administrador; no `/admin`, a tabela mostra E-mail, Papel, Plano, Cadastro e Ações, os contadores são Contas cadastradas / Assinantes ativos / Administradores, não há botão de liberar ou rejeitar cadastro, e criar acesso pelo painel continua funcionando.
