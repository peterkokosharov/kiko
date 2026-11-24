/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * kiko-tray.ini parser and %VAR% expansion.
 */
#include <stdlib.h>
#include <string.h>

#include "kt_ini.h"
#include "kt_test.h"

static const char *lookup(const char *name, void *ctx)
{
    (void)ctx;
    if (strcmp(name, "APPDATA") == 0) return "C:\Users\x\AppData";
    if (strcmp(name, "EMPTY") == 0) return "";
    return NULL;
}

static void check_get(const char *text, const char *key, const char *want)
{
    char *v = kt_ini_get(text, key);
    if (want) CHECK_STR(v, want); else CHECK(v == NULL);
    free(v);
}

static void test_get(void)
{
    const char *t = "\xEF\xBB\xBF# comment\r\n; other\r\n"
                    "command = \"C:\py\python.exe\" \"C:\k\kiko_chain.py\"  \r\n"
                    "  PORT=4001\n"
                    "token_file =%USERPROFILE%\t\n"
                    "empty =\n"
                    "#hidden = no\n"
                    "port = 9\n"
                    "last=x";
    check_get(t, "command", "\"C:\py\python.exe\" \"C:\k\kiko_chain.py\"");
    check_get(t, "port", "4001");                 /* case-insensitive key, first match wins */
    check_get(t, "token_file", "%USERPROFILE%\t");
    check_get(t, "empty", "");
    check_get(t, "hidden", NULL);                 /* commented out */
    check_get(t, "last", "x");                    /* no trailing newline */
    check_get(t, "tok", NULL);                    /* prefix of a key is not a match */
    check_get("portx=1\n", "port", NULL);
    check_get("", "port", NULL);
    check_get(NULL, "port", NULL);
}

static void test_expand(void)
{
    char *s = kt_expand_vars("%APPDATA%\k\%NOPE%\%EMPTY%x 100% %", lookup, NULL);
    CHECK_STR(s, "C:\Users\x\AppData\k\%NOPE%\x 100% %");
    free(s);
    s = kt_expand_vars("plain", lookup, NULL);
    CHECK_STR(s, "plain");
    free(s);
    s = kt_expand_vars("%%", lookup, NULL);
    CHECK_STR(s, "%%");
    free(s);
}

int main(void)
{
    test_get();
    test_expand();
    KT_TEST_DONE();
}
