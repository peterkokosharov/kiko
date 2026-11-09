/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * qt_main.cpp - kiko-tray for KDE Plasma (and any desktop with Qt): QSystemTrayIcon on the
 * StatusNotifierItem protocol.  UNVERIFIED: never compiled.
 * Environment: KIKO_PORT (default 4000), KIKO_API_TOKEN or the file ~/.kiko/api-token.
 */
#include <QApplication>
#include <QByteArray>
#include <QDir>
#include <QFile>

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "qt_internal.h"

#ifndef KT_VERSION
#define KT_VERSION "0.1.0"
#endif

/* First line of ~/.kiko/api-token; the value is never logged. */
static QByteArray read_token_file()
{
    QFile f(QDir::homePath() + QStringLiteral("/.kiko/api-token"));
    if (!f.open(QIODevice::ReadOnly)) return QByteArray();
    return f.readLine(256).trimmed();
}

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    app.setQuitOnLastWindowClosed(false);   /* closing a dialog must not end the tray */
    kt_net_startup();

    int port = 4000;
    const QByteArray envPort = qgetenv("KIKO_PORT");
    if (envPort.toInt() > 0 && envPort.toInt() < 65536) port = envPort.toInt();
    QByteArray token = qgetenv("KIKO_API_TOKEN");
    const char *tokenSource = "env KIKO_API_TOKEN";
    if (token.isEmpty()) {
        token = read_token_file();
        tokenSource = token.isEmpty() ? "none" : "file ~/.kiko/api-token";
    }

    kt_view view;
    if (qt_view_create(&view) != 0) return 1;
    if (!view.claim_single_instance(view.ctx)) {
        std::fprintf(stderr, "kiko-tray is already running\n");
        qt_view_destroy();
        return 0;
    }

    kt_config cfg;
    std::memset(&cfg, 0, sizeof cfg);
    cfg.port = port;
    cfg.api_token = token.isEmpty() ? nullptr : token.constData();
    cfg.token_source = tokenSource;
    cfg.command_text = "systemctl --user start|stop|restart kiko-lite";
    cfg.version = KT_VERSION;
    cfg.poll_interval_ms = 5000;
    cfg.start_grace_ms = 30000;
    kt_host host;
    host.ctx = nullptr;
    host.service = qt_host_service;

    kt_presenter *p = kt_presenter_new(&view, &host, &cfg);
    if (!p || kt_presenter_start(p) != 0) {
        std::fprintf(stderr, "kiko-tray: could not start\n");
        kt_presenter_free(p);
        qt_view_destroy();
        return 1;
    }
    view.run(view.ctx);
    kt_presenter_stop(p);
    kt_presenter_free(p);
    qt_view_destroy();
    kt_secure_zero(token.data(), static_cast<size_t>(token.size()));
    return 0;
}
