> Това е превод на GITHUB-DEPLOY.md. Английският оригинал е меродавен.

# Публикуване на kiko-lite в собствения ти GitHub (за човек и за AI агент)

**Автор:** Peter Kokosharov

## Част А. За човека (около 5 минути)

1. На github.com (в браузъра) създай ново ПРАЗНО ЧАСТНО (PRIVATE) хранилище (repository), например `kiko-lite`.
   Не добавяй README, .gitignore или лиценз.
2. Създай fine-grained token (токен с фини права): Settings > Developer settings > Personal access tokens > Fine-grained tokens.
   (Settings = Настройки; Developer settings = Настройки за разработчици; Personal access tokens = Лични токени за достъп.)
   Repository access: "Only select repositories" (само избрани хранилища), избери САМО това едно хранилище.
   Permissions: Contents = Read and write (четене и писане). Expiration: избери "Custom" и сложи утрешна дата (няма готова опция "1 day").
3. Дай на AI агента: zip файла на kiko-lite, токена, потребителското си име в GitHub и името на хранилището.
4. ВНИМАНИЕ: всичко, което дадеш на облачен AI, се изпраща към сървърите на тази компания. Давай само този
   краткотраен токен, ограничен до едно хранилище. НИКОГА classic токен (класически токен), НИКОГА паролата си.
5. Когато агентът съобщи, че е успял, веднага отмени токена: Settings > Developer settings > Personal access
   tokens > Fine-grained tokens > токена > Delete (Изтрий).

## Част Б. За AI агента (следвай точно, в Linux/macOS shell)

На Windows използвай Git Bash. Трябва git 2.31 или по-нов.
Правила: никога не отпечатвай токена, никога не го слагай в URL или във файл, никога не ползвай `git remote add`
с него, никога не прави force-push. Замени `<user>`, `<repo>` и `<version>` с истинските стойности.

1. Дръж токена само в паметта (не го показвай с echo). Първият ред пречи на shell-а да запише токена във
   файла с историята:

        set +o history; unset HISTFILE
        export GH_TOKEN='<token>'

2. Направи header-а веднъж; не трябва да е празен:

        AUTH=$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')
        [ -n "$AUTH" ] || echo "ERROR: empty header, check GH_TOKEN"

3. Разархивирай в нова празна папка:

        mkdir kiko-lite-publish && cd kiko-lite-publish
        unzip ../kiko-lite-<version>.zip

4. Провери за тайни данни. Командата не трябва да отпечата нищо; ако отпечата нещо, СПРИ и кажи на човека:

        find . \( -name '.env*' -o -name '*.db' -o -name '*.sqlite' -o -name '*.pem' -o -name '*.key' \
        -o -name '*.p12' -o -name '*.pfx' -o -name 'id_rsa*' -o -name 'id_ed25519*' -o -name '.netrc' \
        -o -name 'credentials*.json' \) -print

5. Създай хранилището и направи commit (авторът е фиксиран):

        git init -b main      # git older than 2.28: git init && git checkout -b main
        git add -A
        git -c user.name="Peter Kokosharov" -c user.email="peter.kokosharov@gmail.com" commit -m "kiko-lite: first import"

6. Изпрати (push). Токенът отива в еднократен header, така че не се записва в `.git/config` или в URL.
   `GIT_TERMINAL_PROMPT=0` и `-c credential.helper=` спират въпросите и запазването на пароли (и в Git
   Credential Manager на Windows):

        GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
        git -c credential.helper= push https://github.com/<user>/<repo>.git main

7. Провери. Sha-то, показано за `refs/heads/main`, трябва да е равно на локалното:

        GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
        GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH" \
        git -c credential.helper= ls-remote https://github.com/<user>/<repo>.git
        git rev-parse HEAD

8. Почисти: изпълни `unset GH_TOKEN AUTH`.
9. Докладвай на човека: URL на хранилището `https://github.com/<user>/<repo>`, sha на commit-а и този ред:
   "Please revoke the token now (Settings > Developer settings > Personal access tokens > Fine-grained tokens > (token) > Delete)."
   (Значение: "Моля, отмени токена сега".)

## Отстраняване на проблеми

* **401**: токенът е грешен или е изтекъл. Човекът трябва да направи нов токен.
* **403**: токенът има грешни права или не е за това хранилище. Човекът трябва да създаде нов
  fine-grained токен точно за това хранилище с Contents = Read and write.
* **404** (хранилището не е намерено): потребителското име или името на хранилището е грешно, или токенът не
  вижда това частно хранилище. Провери изписването заедно с човека.
* **non-fast-forward / rejected**: хранилището не е празно. НЕ прави force-push и НЕ прави pull. Спри и питай
  човека.
