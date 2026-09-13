# Prompt de Reprodução — Servidor WoW 3.3.5a no Proxmox

Copie o bloco abaixo inteiro e cole em um agente (Hermes, Claude Code, Codex...)
que tenha acesso SSH ao host Proxmox. Ele reconstrói o ambiente do zero.

O prompt é **autocontido**: o agente não sabe nada sobre esta conversa.

---

```
Tarefa: provisionar um servidor privado World of Warcraft 3.3.5a (TrinityCore)
em uma VM nova no host Proxmox `pv1` (192.168.1.75), com monitoração Prometheus/Grafana.

## Contexto do ambiente

Host Proxmox:
- `pv1` @ 192.168.1.75, SSH root já autorizado, bridge `vmbr0` (192.168.1.0/24)
- Storage: `local` (dir /var/lib/vz) e `local-lvm` (lvm-thin)
- Imagem cloud-init já presente: /var/lib/vz/template/iso/noble-server-cloudimg-amd64.img
- Já existem: VM201 docker-stack @192.168.1.60, VM202 homeassistant @192.168.1.225,
  LXC203 pihole @192.168.1.70, VM200 hermes. NÃO toque nelas.

Stack de monitoração já existente (na VM201):
- Prometheus em http://192.168.1.60:9091 (config em /opt/pandora/prometheus/prometheus.yml)
- Grafana em http://192.168.1.60:3001 (dashboards provisionados por arquivo em
  /opt/pandora/grafana/dashboards/ — são importados automaticamente em ~30s)

Alvo a criar:
- VM nova: 4 vCPU, 6 GB RAM, 50 GB disco, IP 192.168.1.64 (confirme que está livre
  com `ping -c1 -W1 <ip>` antes), Ubuntu 24.04, Docker

## Ordem de execução

### 1. VM no Proxmox (rodar no host pv1)

    qm create 100 --name wow-server --memory 6144 --cores 4 --ostype l26 \
      --machine q35 --scsihw virtio-scsi-pci --net0 virtio,bridge=vmbr0 \
      --serial0 socket --agent enabled=1 --onboot 1
    qm importdisk 100 /var/lib/vz/template/iso/noble-server-cloudimg-amd64.img local-lvm -format raw
    qm set 100 --scsi0 local-lvm:vm-100-disk-0,discard=on
    qm resize 100 scsi0 50G
    qm set 100 --ide2 local:cloudinit --boot order=scsi0
    qm set 100 --ipconfig0 ip=192.168.1.64/24,gw=192.168.1.1 --nameserver 192.168.1.1 \
      --ciuser root --sshkeys /root/.ssh/authorized_keys
    qm set 100 --cpu host        # OBRIGATÓRIO — veja "Armadilhas" abaixo
    qm start 100

Espere o SSH responder (o primeiro boot do cloud-init REINICIA uma vez — repita a
tentativa, não assuma falha):

    for i in $(seq 1 30); do ssh -o ConnectTimeout=2 root@192.168.1.64 'echo READY' 2>/dev/null && break; sleep 5; done

### 2. Docker na VM

    ssh root@192.168.1.64 'curl -fsSL https://get.docker.com | sh'

### 3. Cliente WoW 3.3.5a e TDB

Na máquina que tem espaço em disco (o agente/host):

    ostree=  # n/a
    apt-get install -y aria2
    mkdir -p /root/wow-client
    aria2c --seed-time=0 -d /root/wow-client \
      'magnet:?xt=urn:btih:5b65d1928a3025a820b45e6db2451aaaabc5347c&dn=World%20of%20Warcraft%203.3.5a&xl=17855168814&tr=udp%3A%2F%2Ftracker.openbittorrent.com%3A80%2Fannounce&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce'

Copia pro VM (tar por SSH evita prompts de aprovação de scp):

    cd "/root/wow-client/World of Warcraft 3.3.5a" && tar czf - . | \
      ssh root@192.168.1.64 'mkdir -p /opt/wow-server/client && cd /opt/wow-server/client && tar xzf -'

TDB (dump base do mundo). Baixe a versão EXATA que o binário espera — veja a
Armadilha 2. Para descobrir qual é, rode o passo 5, veja a mensagem de erro, e use
o nome de arquivo citado:

    mkdir -p /opt/wow-server/tdb && cd /opt/wow-server/tdb
    curl -sL -o tdb.7z https://github.com/TrinityCore/TrinityCore/releases/download/TDB335.25101/TDB_full_world_335.25101_2025_10_21.7z
    docker run --rm -v /opt/wow-server/tdb:/tdb --entrypoint /bin/sh \
      danielsilvestre37/trinitycore-docker:3.3.5 -c 'cd /tdb && 7z x tdb.7z -y'

### 4. Deploy do stack

Copie o docker-compose.yml do repo Cividati/wow-server para /opt/wow-server/ (ele já
contém a montagem do TDB e os limites de memória corretos), ajuste
PUBLIC_IP_ADDRESS se o IP for outro, e:

    cd /opt/wow-server && docker compose up -d

### 5. Acompanhar o bootstrap

O bootstrap roda uma vez: cria os schemas, baixa/aplica o TDB, extrai mapas do
cliente e sobe authserver + worldserver. A extração leva ~30 min em 4 vCPU
(mmaps sozinho ~29 min).

    cd /opt/wow-server && docker compose logs -f trinitycore-wowserver

Esperado no fim: "MMaps were built in ...", "Application database update successful",
"World initialized in 0 minutes 7 seconds", "worldserver-daemon) ready..." e prompt TC>.

### 6. Monitoração (host + containers)

    mkdir -p /opt/monitoring
    # copie monitoring/docker-compose.yml do repo para /opt/monitoring/
    cd /opt/monitoring && docker compose up -d

Adicione ao /opt/pandora/prometheus/prometheus.yml na VM201 (192.168.1.60):

      - job_name: "node_wow"
        scrape_interval: 15s
        static_configs:
          - targets: ["192.168.1.64:9100"]
            labels: {host: "wow-server"}

      - job_name: "cadvisor_wow"
        scrape_interval: 30s
        static_configs:
          - targets: ["192.168.1.64:8080"]
            labels: {host: "wow-server"}

Reinicie o Prometheus (SIGHUP NÃO relê o config) e confirme health `up`:

    ssh root@192.168.1.60 'cd /opt/pandora && docker compose restart prometheus'
    ssh root@192.168.1.60 'curl -s "http://localhost:9091/api/v1/targets?state=active" | grep -o "wow" | head'

Dashboard do Grafana: coloque o JSON em /opt/pandora/grafana/dashboards/ (arquivo
cru, sem wrapper). O Grafana importa em ~30s. NÃO tente a API — veja Armadilha 4.

### 7. Criar conta e verificar

Contas são criadas no servidor. Sem TTY, use o socket.io do web UI
(scripts/wow_console.py no repo):

    python3 scripts/wow_console.py 'account create <usuario> <senha>' 'account set gmlevel <usuario> 3 -1'

Verifique: "TC> Account created: <usuario>" e "Security level of account <USUARIO> changed to 3."
Confirme no banco:

    ssh root@192.168.1.64 'docker exec trinitycore-db mysql -uroot -ptrinityroot \
      -e "SELECT id,username,expansion FROM auth.account;"'
    ssh root@192.168.1.64 'docker exec trinitycore-db mysql -uroot -ptrinityroot \
      -e "SELECT * FROM auth.account_access;"'

(usuários são gravados em MAIÚSCULO; expansion 2 = WotLK; account_access usa
SecurityLevel, não gmlevel)

Cliente: em Data/<locale>/realmlist.wtf (enUS/ptBR/ptPT) põe:

    set realmlist 192.168.1.64
    set patchlist 192.168.1.64

e abre Wow.exe DIRETO (nunca o launcher — ele tenta patch e quebra a compatibilidade).

## Armadilhas (todas custaram tempo — não repita)

1. **MySQL 8.4.4 exige CPU x86-64-v2.** A VM criada com o CPU default do Proxmox
   (`kvm64`) NÃO tem essa feature: o container `trinitycore-db` entra em loop com
   "Fatal glibc error: CPU does not support x86-64-v2", o healthcheck nunca fica
   saudável e o wowserver nem inicia. Solução: `qm set <id> --cpu host` + REINICIAR
   a VM (`qm reboot`) — mudança de modelo de CPU não aplica a quente.

2. **A imagem baixa o TDB errado.** Dois defeitos somados no bootstrap da imagem:
   (a) `Database.containsData()` checa SÓ o banco `auth` — se `auth` tem tabelas e
   `world` está vazio, ele pula o download do seed; (b) `downloadInitialData()` pega
   o TDB MAIS NOVO dos releases da TrinityCore, mas o binário `worldserver` foi
   compilado esperando um nome de arquivo ESPECÍFICO. Resultado: "Could not populate
   the World database" → processo sai com código 1 → o Node morre por unhandled
   rejection → o container entra em restart loop. Solução: bind-mount do .sql com o
   nome exato em /app/server/bin (como está no docker-compose.yml).
   `/app/server/bin` NÃO é volume — cópia simples é apagada no recreate.

3. **O bootstrap engole o stdout do worldserver.** O `CommandExecuter` captura a
   saída para o tracker e o `docker logs` só mostra o crash do Node, nunca o erro
   real. Para ver o motivo de verdade, rode o binário na mão:

       docker run -d --name wow-debug --network wow-server_default \
         -v wow-server_server_data:/app/server/data -v wow-server_server_logs:/app/server/logs \
         -v /opt/wow-server/client:/app/client -e PUBLIC_IP_ADDRESS=192.168.1.64 \
         --entrypoint /bin/sh danielsilvestre37/trinitycore-docker:3.3.5 -c "sleep infinity"
       docker exec wow-debug sh -c 'sed -e "s|<DATABASE_HOST>|database|g" -e "s|<DATABASE_PORT>|3306|g" \
         -e "s|<DATABASE_USER>|trinity|g" -e "s|<DATABASE_PASSWORD>|trinity|g" \
         /app/backend/resources/worldserver.335.conf.dist > /app/server/etc/worldserver.conf'
       # (suba o banco antes) então:
       docker exec wow-debug sh -c 'cd /app/server/bin && ./worldserver -u 2>&1 | head -20'

4. **Grafana: `admin:admin` não funciona.** A senha é trocada em runtime depois do
   primeiro boot (o `GF_SECURITY_ADMIN_PASSWORD` só vale na criação do DB) e, com
   anonymous Viewer ligado, o write na API cai em "Access denied". Use provisioning
   por arquivo (`/opt/pandora/grafana/dashboards/*.json`) — mais simples e
   versionável. Se precisar mesmo da API:
   `docker exec -u root grafana grafana cli admin reset-admin-password <nova>`.

5. **Porta aberta != servidor pronto.** O `docker-proxy` binda 3724/8085/3000 assim
   que o container sobe, então um teste de TCP dá FALSO POSITIVO durante a extração.
   Cheque os processos: `docker exec trinitycore-wowserver pgrep -x worldserver`
   (idem authserver / mmaps_generator).

6. **Limites de memória têm que caber na VM.** Um `limits: memory: "8g"` numa VM de
   6 GB é insatisfazível. Este repo usa 5g (wowserver) + 1g (MySQL).

7. **`docker compose down -v` nem sempre limpa.** Confirme com
   `docker volume ls | grep wow` — um `db_data` remanescente mantém tabelas em
   `auth` e continua enganando o check (bug 2a).

## Critério de pronto

- `docker compose ps`: `trinitycore-db` healthy e `trinitycore-wowserver` Up (não "Restarting")
- `docker exec trinitycore-wowserver pgrep -x worldserver` e `... authserver` retornam PID
- `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='world'` >> 0 (centenas)
- 192.168.1.64:3724 e :8085 aceitam conexão de outra máquina da LAN
- Prometheus: jobs `node_wow` e `cadvisor_wow` com health `up`
- Uma conta criada e confirmada em `auth.account`
```

---

## Referências

- Repo: https://github.com/Cividati/wow-server
- Imagem: `danielsilvestre37/trinitycore-docker:3.3.5` (fonte: https://github.com/valcriss/trinitycore-docker)
- TDB releases: https://github.com/TrinityCore/TrinityCore/releases
- Docs TrinityCore 3.3.5: https://335.trinitycore.net/
