# STRYKE BOT

Bot de Discord em Python com painel permanente, tickets privados de Farme e registro de ausência.

## Requisitos

- Windows 10/11
- Python 3.10 ou mais recente (`py --version` ou `python --version`)
- Aplicação criada no Discord Developer Portal e token de bot
- Permissões adequadas para o cargo do bot no servidor

## Instalação local

No PowerShell, dentro desta pasta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Preencha `DISCORD_TOKEN` com o token do bot e `DISCORD_CLIENT_ID` com o Application ID. Não compartilhe `.env`, não cole o token no chat e regenere-o no portal se for exposto. `DISCORD_GUILD_ID` é opcional; quando preenchido, os comandos de barra são sincronizados diretamente no servidor. Sem ele, a sincronização é global e pode demorar para propagar.

Inicie com:

```powershell
python main.py
```

Mantenha o processo aberto para o bot permanecer online. Para reiniciar, encerre com Ctrl+C e execute novamente.

### Iniciar no Windows de outro computador

O arquivo `deploy/stryke-bot-windows.zip` inclui o código e `deploy/start-windows.bat`, sem `.env` ou arquivos de dados. Extraia o ZIP, instale Python 3.10+ marcando **Add Python to PATH**, execute `deploy/start-windows.bat`, preencha o token e o Application ID no `.env` quando o Bloco de Notas abrir e rode o iniciador outra vez. A janela do terminal precisa permanecer aberta; o script tenta reiniciar o bot se o processo encerrar. Configure Windows para não suspender o PC e mantenha internet e energia. Para migrar os dados, copie `bot_state.json` e `meta_state.json` em particular para a pasta junto de `main.py`. Só inicie no outro PC depois de parar a instância atual.

## Developer Portal e instalação

1. Em **Bot**, gere/copiei o token e habilite o bot. Para capturar identificações enviadas manualmente, ative **Message Content Intent** e defina `CAPTURE_MANUAL_IDENTIFICATIONS=true` no ambiente do bot. O código só arquiva mensagens do canal `📝 identificação`; não habilite intents de membros ou presença. Enquanto a intenção/configuração não estiverem ativas, as mensagens antigas ficam preservadas e o canal não é limpo.
2. Em **OAuth2 → URL Generator**, selecione os escopos `bot` e `applications.commands`.
3. Permissões necessárias: View Channels, Manage Channels, Manage Roles, Manage Nicknames, Send Messages, Read Message History e Embed Links. O cargo do bot deve ficar acima de `Membro` na hierarquia do servidor. Attach Files não é necessário para o bot: o membro pode enviar prints nos tickets privados. Não selecione Administrator. Algumas telas do Discord podem requerer permissões administrativas próprias para autorizar a instalação no servidor.
4. Abra o link gerado, escolha o servidor correto e confirme a autorização. A instalação exige que sua conta tenha permissão para adicionar aplicativos.
5. O Application ID da aplicação STRYKE.BOT já está configurado localmente em `DISCORD_CLIENT_ID`. O link preparado, com os escopos `bot` e `applications.commands` e as permissões necessárias, é: https://discord.com/oauth2/authorize?client_id=1558161268748324935&permissions=402738192&scope=bot%20applications.commands. Abra-o, escolha o servidor e confirme a instalação se precisar conceder permissões novas. Nunca copie o token para o código.

O projeto não usa APIs não oficiais nem automatiza login/autorização. A autorização no portal e a seleção do servidor são feitas pelo proprietário/admin.

## Configuração do servidor

Ao conectar, o bot procura `『🤖』bot-abrir-chamado`, `『😇』AUSENCIAS` e `📝 identificação` (por nome normalizado ou pelos IDs no `.env`). No canal de identificação, publica ou atualiza um painel com botão para abrir o formulário. A categoria de Farme é reconhecida quando há uma única categoria com “farme” no nome. Se houver mais de uma ou nenhuma, informe o ID escolhido em `FARME_CATEGORY_ID` no `.env`.

O dono de cada ticket e os cargos `🚀 LÍDER`, `🔥 SUB LÍDER` e `[RESP] FARME` podem ver e enviar mensagens, inclusive consultar todo o histórico. Outros membros não veem o ticket. Ao iniciar, o bot também corrige as permissões dos tickets Farme já existentes. Se houver cargos duplicados com esses nomes, todos os cargos com o nome correspondente recebem acesso; IDs específicos podem ser configurados em `ROLE_LEADER_ID`, `ROLE_SUB_LEADER_ID` e `ROLE_FARME_RESP_ID`. O bot precisa estar acima dos cargos de Farme na hierarquia e ter Manage Channels, View Channels, Send Messages, Read Message History, Embed Links e Attach Files. Não altere as permissões existentes do canal de ausências.

Ao iniciar, o painel é criado ou atualizado no canal indicado e sua mensagem é registrada em `bot_state.json` (arquivo local ignorado pelo Git). Qualquer membro do servidor pode usar `/painel` para repetir essa atualização sem criar mensagens duplicadas. O bot remove cópias globais antigas dos comandos e mantém os registros do servidor configurado, evitando duplicidades como dois `/painel`.

A ausência usa um Modal. Após envio válido, o bot publica o registro no canal indicado, cria um canal privado temporário para a pessoa e agenda a exclusão desse canal após 15 minutos. O registro publicado não é apagado. Se o processo do bot parar durante esses 15 minutos, a tarefa agendada não sobrevive ao reinício.

## Meta da família

Um administrador com **Manage Server** executa `/painel-meta` somente no canal `『🕹️』ᴄᴏɴᴛᴏʟᴇ-ᴅᴇ-ꜰᴀʀᴍᴇ` (definido por `META_PANEL_CHANNEL_ID`). O painel mostra o total semanal registrado no bot e o acumulado de valores dos cadastros do site, sem listar os valores por membro. O site é consultado a cada minuto e também no clique em **Atualizar painel**. O botão **Registrar valor** aceita valores positivos em reais, como `1500`, `1.500,00` ou `1500.50`, com até duas casas decimais. O comando `/registrar-farme` continua registrando as entregas nos tickets individuais.

O ciclo começa com uma meta familiar de R$ 100.000,00. Ao concluí-la, o bot publica uma mensagem de agradecimento e incentivo no canal do painel e inicia automaticamente a etapa pessoal do zero. Nessa etapa, os valores ficam separados por membro e não há um limite fixo; a contagem continua até um cargo autorizado resetar tudo. O painel público mostra somente o total geral da etapa pessoal; cada membro recebe seu próprio total em uma confirmação privada. O botão **Resetar** pede confirmação, arquiva os valores da etapa e inicia um novo ciclo familiar do zero. Valores que ultrapassem o restante da meta familiar são recusados; depois de concluí-la, registre os próximos valores na etapa pessoal. Os dados ficam em `meta_state.json`, ignorado pelo Git e preservado entre reinicializações.

O total semanal é salvo em `meta_state.json`. Cada período vai de segunda-feira às 14h até a segunda seguinte às 14h; os controles no canal da meta permitem atualizar o painel e, após o prazo, fechar a semana, arquivar o total e abrir a próxima semana numerada. Os registros antigos não tinham data individual, então não é possível reconstruir o total desta semana antes da atualização; o acumulado semanal começa a partir dela. O ciclo familiar de R$ 100.000, a etapa pessoal e o reset autorizado continuam funcionando separadamente.

O fechamento semanal exige os cargos autorizados para resetar a meta. Após a confirmação, o bot publica um aviso com `@everyone` em `#informativos` e envia o mesmo lembrete aos donos dos tickets Farme que estiverem abertos. Tickets Farme criados depois já recebem o aviso da semana atual na mensagem inicial.

## Formulário de identificação

No canal público `📝 identificação`, o bot mantém somente o painel **Preencher identificação**. Os cadastros são publicados no canal privado `#identificacoes`, que o bot cria se não existir. O acesso de leitura é dado ao cargo `Gerente`/`Gerentes` (ou `Manager`/`Managers`) e a todos os cargos acima dele; `@everyone` não consegue ver o canal. Se o cargo tiver outro nome, configure `IDENTIFICATION_MANAGER_ROLE_ID` com o ID do cargo.

Quando o bot iniciar com `CAPTURE_MANUAL_IDENTIFICATIONS=true` e **Message Content Intent** ativo, ele copia os cadastros do formulário e as mensagens enviadas manualmente no canal público para o canal privado. Nas mensagens manuais, preserva texto, anexos e embeds junto com o nome e ID de quem enviou. Só depois de confirmar o arquivamento, apaga as outras mensagens do canal público, mantendo o painel. Depois da inicialização, novas identificações manuais são arquivadas no canal privado e removidas do canal público; se a configuração estiver desligada ou o arquivo falhar, a mensagem original permanece. Se não conseguir identificar o cargo de gerente ou proteger o canal, o bot não limpa o canal público. Novos cadastros do formulário vão diretamente para `#identificacoes`, atribuem o cargo `Membro` e definem o apelido com o campo **Nome** (até 32 caracteres). O contador continua salvo entre reinicializações.

O bot precisa de Ver canal, Ver histórico de mensagens e Enviar mensagens/links no canal público, Gerenciar canais e Gerenciar cargos para criar e proteger `#identificacoes`, além de Gerenciar cargos e Gerenciar apelidos para concluir a identificação. O cargo do bot precisa ficar acima do cargo `Gerente` e dos cargos cuja permissão de leitura será configurada.

## Hospedar no Google Cloud Free Tier

O bot pode rodar continuamente em uma VM Compute Engine `e2-micro` elegível ao Free Tier, em `us-west1`, `us-central1` ou `us-east1`. A conta de faturamento é necessária. Atenção: a VM pode estar coberta pelo Free Tier, mas o IPv4 externo em uso é cobrado à parte (o benefício gratuito de IPv4 cobre somente uma hora por mês), então esta opção não garante custo zero. Confira os limites e preços atuais antes de criar a VM.

O pacote `deploy/stryke-bot-gcp.zip` contém apenas código e configuração, sem `.env` nem dados locais. Na VM Ubuntu, extraia o pacote em `/opt/stryke-bot`, crie `/etc/stryke-bot.env` com as variáveis do bot (permissão `600`) e, antes de iniciar o serviço, copie os estados antigos para `/opt/stryke-bot/bootstrap-state/` para serem importados. Execute `sudo bash /opt/stryke-bot/deploy/install-gcp.sh`; esse script instala as dependências, cria o usuário de serviço, importa os estados se presentes e instala/ativa o serviço systemd.

Antes de desligar o bot local, faça uma cópia de `bot_state.json` e `meta_state.json` para `/var/lib/stryke-bot` na VM, mantendo os nomes dos arquivos. Rode somente uma instância por vez para evitar conexões duplicadas e alterações concorrentes nos arquivos. Consulte logs com `sudo journalctl -u stryke-bot -f`.

O reset exige um dos cargos em `META_ADMIN_ROLE_IDS` quando essa lista está preenchida. Se a lista não estiver definida, o bot aceita `META_ADMIN_ROLE_ID`; na ausência de ambos, somente membros com **Manage Server** ou **Administrator** podem resetar. O arquivo local deste servidor contém a lista explícita de cargos autorizados.

## Hospedar sem mensalidade em um Android com Termux

Esta opção não gera uma conta de hospedagem, mas o bot depende do celular, da energia e da internet. Android pode encerrar processos em segundo plano; Termux:Boot e Wake Lock ajudam a manter/iniciar o processo, mas não garantem disponibilidade como um servidor.

1. Instale **Termux** e **Termux:Boot** da mesma origem oficial (F-Droid ou GitHub do Termux; não misture origens). Abra Termux uma vez e abra Termux:Boot uma vez.
2. Copie `deploy/stryke-bot-termux.zip` para o telefone, extraia `stryke-bot/` para `$HOME/stryke-bot` dentro do Termux e rode `bash ~/stryke-bot/deploy/install-termux.sh`.
3. Edite `~/stryke-bot/.env` com `nano ~/stryke-bot/.env` e coloque o token diretamente no telefone. Não envie o token em mensagens. Copie também `bot_state.json` e `meta_state.json` do computador para `~/stryke-bot/` se quiser preservar os registros atuais.
4. Inicie com `bash ~/stryke-bot/deploy/start-termux.sh`. Para iniciar após reinicializar o Android, o script já é instalado em `~/.termux/boot/` pelo instalador.
5. Nas configurações Android, permita atividade em segundo plano e remova a otimização de bateria do Termux e do Termux:Boot. Mantenha o telefone conectado ao Wi-Fi/energia; `termux-wake-lock` aumenta o consumo de bateria.

Faça a migração com apenas uma instância conectada ao Discord: pare a instância do PC antes de iniciar a do celular. O arquivo ZIP não contém o `.env` nem os arquivos de estado; transfira os estados por um meio privado. Termux:Boot só roda o script depois que o telefone reiniciar e o usuário desbloquear o aparelho, dependendo da versão do Android.

## Limpar tickets

Nos canais de ticket Farme criados pelo bot, qualquer participante com acesso ao canal pode usar `/clear`; ele remove todas as mensagens posteriores à mensagem inicial e mantém apenas essa mensagem no canal. Isso reinicia as mensagens do ticket, sem alterar os ciclos da meta. O bot precisa de **Gerenciar mensagens** e **Ver histórico de mensagens** no ticket.

Qualquer pessoa com acesso ao ticket pode usar `/registrar-farme` para informar quem recebeu a entrega, o valor e o que foi colocado no baú. O bot publica um registro no próprio ticket; os prints obrigatórios devem ser enviados como anexos no canal.

## Permissões OAuth2

O bot usa `bot` + `applications.commands`. Permissões: View Channels, Manage Channels, Manage Roles, Manage Nicknames, Send Messages, Read Message History e Embed Links (inteiro de permissões `402738192`). Mantenha o cargo do bot acima de `Membro`, `Gerente` e dos cargos superiores para que ele possa configurar o canal privado de identificação. Não precisa de Administrator. Para capturar mensagens manuais, ative **Message Content Intent** em Developer Portal → Bot e defina `CAPTURE_MANUAL_IDENTIFICATIONS=true`; embora a intenção habilite o recebimento de conteúdo de mensagens do servidor, o código só processa o canal `📝 identificação`. O escopo `applications.commands` permite os comandos de barra.

## Diagnóstico

- `DISCORD_TOKEN não está configurado`: crie `.env` a partir do exemplo e preencha localmente.
- Canais/cargos não encontrados: confirme os nomes ou o acesso do bot no servidor.
- Sem permissão ao criar canais: confira Manage Channels e a posição do cargo do bot.
- Botões persistem via `custom_id` estável e `View(timeout=None)`; o processo precisa reconectar ao Discord.

Nenhum teste real no Discord é alegado até que o bot conecte a uma aplicação e servidor autorizados.
