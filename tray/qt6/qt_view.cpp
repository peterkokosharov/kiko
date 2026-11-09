/* SPDX-License-Identifier: MIT
 * Author: Peter Kokosharov <peter.kokosharov@gmail.com>
 *
 * qt_view.cpp - Qt Widgets layer (Qt6, or Qt5 as fallback).  QSystemTrayIcon (StatusNotifierItem on
 * KDE Plasma), QMenu, QDialog with QTableWidget / read-only QPlainTextEdit.  No Q_OBJECT classes, so
 * no moc step is needed.  post_to_ui uses a queued invokeMethod.  UNVERIFIED: never compiled.
 * All kt_view calls except post_to_ui and quit run on the Qt main thread.
 */
#include <QAction>
#include <QApplication>
#include <QDialog>
#include <QDir>
#include <QFontDatabase>
#include <QFont>
#include <QHash>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QIcon>
#include <QImage>
#include <QLabel>
#include <QLockFile>
#include <QMenu>
#include <QMetaObject>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QStandardPaths>
#include <QSystemTrayIcon>
#include <QTableWidget>
#include <QVBoxLayout>

#include <cstring>

#include "qt_internal.h"

namespace {

const kt_events *g_events = nullptr;
QSystemTrayIcon *g_tray = nullptr;
QMenu *g_menu = nullptr;
QIcon g_icons[4];
QLockFile *g_lock = nullptr;
QHash<QString, QDialog *> g_dialogs;

QIcon make_icon(kt_state st)
{
    const int size = 64;   /* the desktop scales it down; extra pixels keep it sharp on HiDPI */
    QImage img(size, size, QImage::Format_RGBA8888);
    kt_icon_render(size, st, 0, img.bits());   /* RGBA, straight alpha; rows are 4*size bytes, no padding */
    return QIcon(QPixmap::fromImage(img));
}

QString s(const char *utf8) { return QString::fromUtf8(utf8 ? utf8 : ""); }

void v_bind(void *, const kt_events *e) { g_events = e; }

int v_claim_single_instance(void *)
{
    /* per-user lock file: the runtime dir (XDG_RUNTIME_DIR) when there is one, else the temp dir with the user name */
    QString dir = QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation);
    QString path;
    if (!dir.isEmpty()) path = dir + QStringLiteral("/kiko-tray.lock");
    else path = QDir::tempPath() + QStringLiteral("/kiko-tray-") + qEnvironmentVariable("USER", QStringLiteral("user")) + QStringLiteral(".lock");
    g_lock = new QLockFile(path);
    g_lock->setStaleLockTime(0);   /* a lock left by a crashed tray is not stale-checked by age; PID is checked */
    if (!g_lock->tryLock(0)) { delete g_lock; g_lock = nullptr; return 0; }
    return 1;
}

void v_set_state(void *, kt_state st, const char *tooltip)
{
    if (!g_tray) return;
    g_tray->setIcon(g_icons[st]);
    g_tray->setToolTip(s(tooltip));
    if (!g_tray->isVisible()) g_tray->show();
}

void v_set_menu(void *, const kt_menu_item *items, int n)
{
    QMenu *menu = new QMenu();
    for (int i = 0; i < n; i++) {
        if (items[i].separator) { menu->addSeparator(); continue; }
        QAction *a = menu->addAction(s(items[i].label));
        a->setEnabled(items[i].enabled != 0);
        const QByteArray id = QByteArray(items[i].id ? items[i].id : "");
        QObject::connect(a, &QAction::triggered, menu, [id]() {
            if (!id.isEmpty() && g_events && g_events->on_menu_item) g_events->on_menu_item(g_events->ctx, id.constData());
        });
    }
    if (g_tray) g_tray->setContextMenu(menu);
    QMenu *old = g_menu;
    g_menu = menu;
    if (old) old->deleteLater();
}

void v_notify(void *, const char *text)
{
    if (g_tray) g_tray->showMessage(QStringLiteral("kiko"), s(text), QSystemTrayIcon::Information, 6000);
}

void v_open_key_store(void *)
{
    /* KDE Wallet manager: the executable name differs between Plasma 5 and 6 */
    const char *names[] = { "kwalletmanager6", "kwalletmanager5", "kwalletmanager" };
    for (const char *name : names) {
        if (!QStandardPaths::findExecutable(QString::fromLatin1(name)).isEmpty()) {
            QProcess::startDetached(QString::fromLatin1(name), QStringList());
            return;
        }
    }
    v_notify(nullptr, "KDE Wallet manager not found (install kwalletmanager)");
}

void v_post_to_ui(void *, kt_ui_fn fn, void *arg)
{
    /* queued: runs on the thread of qApp (the main thread); safe to call from any thread */
    QMetaObject::invokeMethod(qApp, [fn, arg]() { fn(arg); }, Qt::QueuedConnection);
}

void v_run(void *) { QApplication::exec(); }

void v_quit(void *)
{
    QMetaObject::invokeMethod(qApp, []() { QApplication::quit(); }, Qt::QueuedConnection);
}

}  // namespace

namespace {

QWidget *make_content(QDialog *dlg, const QString &kind, const kt_dialog *m)
{
    QWidget *root = new QWidget(dlg);
    QVBoxLayout *box = new QVBoxLayout(root);
    box->setContentsMargins(0, 0, 0, 0);

    if (m->summary[0]) {
        QLabel *l = new QLabel(s(m->summary), root);
        l->setWordWrap(true);
        l->setTextInteractionFlags(Qt::TextSelectableByMouse);
        box->addWidget(l);
    }
    if (m->ncols > 0) {
        QTableWidget *t = new QTableWidget(m->nrows, m->ncols, root);
        QStringList heads;
        for (int c = 0; c < m->ncols; c++) heads << s(m->columns[c]);
        t->setHorizontalHeaderLabels(heads);
        for (int r = 0; r < m->nrows; r++)
            for (int c = 0; c < m->ncols; c++) t->setItem(r, c, new QTableWidgetItem(s(m->cells[r * m->ncols + c])));
        t->setEditTriggers(QAbstractItemView::NoEditTriggers);
        t->setSelectionBehavior(QAbstractItemView::SelectRows);
        t->horizontalHeader()->setStretchLastSection(true);
        t->resizeColumnsToContents();
        box->addWidget(t, 1);
    } else {
        QPlainTextEdit *e = new QPlainTextEdit(root);
        e->setReadOnly(true);
        e->setLineWrapMode(QPlainTextEdit::NoWrap);
        e->setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
        e->setPlainText(s(m->text));
        e->moveCursor(QTextCursor::End);   /* newest line (logs) */
        box->addWidget(e, 1);
    }
    if (m->footer[0]) {
        QLabel *l = new QLabel(s(m->footer), root);
        l->setWordWrap(true);
        box->addWidget(l);
    }
    QHBoxLayout *row = new QHBoxLayout();
    for (int i = 0; i < m->nbuttons; i++) {
        QPushButton *b = new QPushButton(s(m->buttons[i].label), root);
        const QByteArray kindUtf8 = kind.toUtf8();
        const QByteArray id = QByteArray(m->buttons[i].id);
        QObject::connect(b, &QPushButton::clicked, dlg, [kindUtf8, id]() {
            if (g_events && g_events->on_dialog_button) g_events->on_dialog_button(g_events->ctx, kindUtf8.constData(), id.constData());
        });
        row->addWidget(b);
    }
    row->addStretch(1);
    QPushButton *close = new QPushButton(QStringLiteral("Close"), root);
    QObject::connect(close, &QPushButton::clicked, dlg, &QDialog::close);
    row->addWidget(close);
    box->addLayout(row);
    return root;
}

void v_show_dialog(void *, const kt_dialog *m)
{
    const QString kind = s(m->kind);
    QDialog *dlg = g_dialogs.value(kind, nullptr);
    if (!dlg) {
        dlg = new QDialog();
        dlg->setAttribute(Qt::WA_DeleteOnClose);
        dlg->resize(680, 460);
        new QVBoxLayout(dlg);
        g_dialogs.insert(kind, dlg);
        const QByteArray kindUtf8 = kind.toUtf8();
        QObject::connect(dlg, &QObject::destroyed, qApp, [kind, kindUtf8]() {
            g_dialogs.remove(kind);
            if (g_events && g_events->on_dialog_closed) g_events->on_dialog_closed(g_events->ctx, kindUtf8.constData());
        });
    }
    dlg->setWindowTitle(s(m->title));
    QLayout *lay = dlg->layout();
    while (QLayoutItem *item = lay->takeAt(0)) {   /* update = rebuild the content */
        if (item->widget()) item->widget()->deleteLater();
        delete item;
    }
    lay->addWidget(make_content(dlg, kind, m));
    dlg->show();
    dlg->raise();
    dlg->activateWindow();
}

}  // namespace

int qt_view_create(kt_view *out)
{
    for (int i = 0; i < 4; i++) g_icons[i] = make_icon(static_cast<kt_state>(i));
    g_tray = new QSystemTrayIcon(g_icons[KT_STATE_STARTING]);
    QObject::connect(g_tray, &QSystemTrayIcon::activated, qApp, [](QSystemTrayIcon::ActivationReason r) {
        if ((r == QSystemTrayIcon::DoubleClick || r == QSystemTrayIcon::Trigger) && g_events && g_events->on_default_action)
            g_events->on_default_action(g_events->ctx);
    });
    g_tray->show();

    std::memset(out, 0, sizeof *out);
    out->key_store_label = "Open KDE Wallet";
    out->bind = v_bind;
    out->claim_single_instance = v_claim_single_instance;
    out->set_state = v_set_state;
    out->set_menu = v_set_menu;
    out->show_dialog = v_show_dialog;
    out->notify = v_notify;
    out->open_key_store = v_open_key_store;
    out->post_to_ui = v_post_to_ui;
    out->run = v_run;
    out->quit = v_quit;
    return 0;
}

void qt_view_destroy()
{
    g_events = nullptr;   /* the presenter is already freed: no callbacks while closing */
    for (QIcon &ic : g_icons) ic = QIcon();   /* global QIcons must not outlive QApplication */
    const QList<QDialog *> open = g_dialogs.values();
    for (QDialog *d : open) delete d;
    if (g_tray) { g_tray->hide(); delete g_tray; g_tray = nullptr; }
    delete g_menu;
    g_menu = nullptr;
    delete g_lock;   /* releases the lock file */
    g_lock = nullptr;
}
