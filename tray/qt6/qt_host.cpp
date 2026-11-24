/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 * AI models: unknown (before 2026-11-24), claude-sonnet-5-5
 *
 * qt_host.cpp - kiko control through the systemd USER unit (default kiko-lite.service, override with env KIKO_UNIT; same as the GTK layer).
 * Runs on a presenter worker thread; the QProcess lives and dies on that thread (no event loop needed).
 */
#include <QProcess>
#include <QString>
#include <QStringList>

#include <cstdio>

#include "qt_internal.h"

extern "C" int qt_host_service(void *ctx, kt_action action, char *msg, size_t msgsz)
{
    (void)ctx;
    const char *verb = action == KT_ACT_START ? "start" : (action == KT_ACT_STOP ? "stop" : "restart");
    const QString unit = qEnvironmentVariableIsEmpty("KIKO_UNIT") ? QStringLiteral("kiko-lite")
                                                                  : qEnvironmentVariable("KIKO_UNIT");
    QProcess p;
    p.start(QStringLiteral("systemctl"),
            QStringList() << QStringLiteral("--user") << QString::fromLatin1(verb) << unit);
    if (!p.waitForStarted(5000)) {
        std::snprintf(msg, msgsz, "Cannot run systemctl");
        return -1;
    }
    if (!p.waitForFinished(120000)) {   /* Type=notify units can take a while to report READY */
        p.kill();
        std::snprintf(msg, msgsz, "systemctl --user %s %s timed out", verb, unit.toUtf8().constData());
        return -1;
    }
    if (p.exitStatus() == QProcess::NormalExit && p.exitCode() == 0) {
        std::snprintf(msg, msgsz, "kiko-lite: %s done", verb);
        return 0;
    }
    const QString first = QString::fromUtf8(p.readAllStandardError()).split(QLatin1Char('\n')).value(0).trimmed();
    std::snprintf(msg, msgsz, "systemctl --user %s %s failed: %s", verb, unit.toUtf8().constData(),
                  first.isEmpty() ? "see journalctl --user -u <unit>" : first.toUtf8().constData());
    return -1;
}
