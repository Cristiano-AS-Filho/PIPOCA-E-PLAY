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
