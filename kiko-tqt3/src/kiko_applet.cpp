/*
 * kiko TDE Panel Applet
 * Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
 * Licensed under MIT License
 */

#include "kiko_applet.h"
#include "kiko_log_panel.h"
#include <tdeconfig.h>
#include <tdeglobal.h>
#include <tdelocale.h>
#include <tdemessagebox.h>
#include <tdefiledialog.h>
#include <kstandarddirs.h>
#include <tqlayout.h>
#include <tqtooltip.h>
#include <tqcursor.h>
#include <kiconloader.h>
#include <tdeapplication.h>
#include <kdebug.h>
#include <unistd.h>

extern "C" {
    KPanelApplet* init(TQWidget* parent, const TQValueList<TQVariant>& args) {
        TQString configFile;
        if (args.count() > 0) {
            configFile = args[0].toString();
        }
        return new KikoApplet(configFile, parent);
    }
}

// tdeinit entry point - required for TDE panel applets (must be outside extern "C" for visibility)
extern "C" TDE_EXPORT void tdeinit_kiko_applet() {
    // Empty - presence is what matters for tdeinit loading mechanism
}

KikoApplet::KikoApplet(const TQString& configFile, TQWidget* parent)
    : KPanelApplet(configFile, KPanelApplet::Normal, 0, parent, "kiko_applet")
    , m_process(new KikoProcess(this))
    , m_menu(new TQPopupMenu(this))
    , m_tooltipTimer(new TQTimer(this))
    , m_iconLabel(new TQLabel(this))
    , m_port(4000)
{
    // Load config
    setupConfig();

    // Load icons from TDE icon system
    m_iconRunning   = TDEGlobal::iconLoader()->loadIconSet("Kiko_running", TDEIcon::Panel);
    m_iconStopped   = TDEGlobal::iconLoader()->loadIconSet("Kiko_stopped", TDEIcon::Panel);
    m_iconError     = TDEGlobal::iconLoader()->loadIconSet("Kiko_error", TDEIcon::Panel);
    m_iconStarting  = TDEGlobal::iconLoader()->loadIconSet("Kiko_idle", TDEIcon::Panel);
    m_iconIdle      = m_iconStopped;  // fallback

    // Fallback: if theme icons not found, try loading from installed path
    if (m_iconRunning.isNull()) {
        TQString iconDir = TDEGlobal::dirs()->findResource("icon", "hicolor/22x22/apps/");
        if (!iconDir.isEmpty()) {
            m_iconRunning   = TQIconSet(TQPixmap(iconDir + "Kiko_running.png"));
            m_iconStopped   = TQIconSet(TQPixmap(iconDir + "Kiko_stopped.png"));
            m_iconError     = TQIconSet(TQPixmap(iconDir + "Kiko_error.png"));
            m_iconStarting  = TQIconSet(TQPixmap(iconDir + "Kiko_idle.png"));
            m_iconIdle      = m_iconStopped;
            kdDebug() << "Loaded icons from: " << iconDir << endl;
        } else {
            kdDebug() << "Icon directory not found in resource system" << endl;
        }
    } else {
        kdDebug() << "Loaded icons from theme system" << endl;
    }

    // UI setup - in Qt3, layout is auto-set when created with parent
    TQHBoxLayout* layout = new TQHBoxLayout(this);
    layout->setMargin(0);
    layout->setSpacing(0);
    layout->addWidget(m_iconLabel);
    m_iconLabel->setPixmap(m_iconStopped.pixmap(TQIconSet::Small, TQIconSet::Normal));
    // No setLayout() needed in Qt3 - parent constructor sets it

    // Process signals - use unscoped enum name as MOC expects
    connect(m_process, TQ_SIGNAL(stateChanged(int)), this, TQ_SLOT(onProcessStateChanged(int)));
    connect(m_process, TQ_SIGNAL(outputReceived(const TQString&)), this, TQ_SLOT(onProcessOutput(const TQString&)));
    connect(m_process, TQ_SIGNAL(errorOccurred(const TQString&)), this, TQ_SLOT(onProcessError(const TQString&)));

    // Tooltip update timer
    connect(m_tooltipTimer, TQ_SIGNAL(timeout()), this, TQ_SLOT(updateTooltip()));
    m_tooltipTimer->start(5000);

    // Context menu
    createMenu();
    setCustomMenu(m_menu);

    // Apply config to process
    m_process->setConfigPath(m_configPath);
    m_process->setPort(m_port);
    m_process->setKikoBinary(m_binaryPath);

    // Initial state
    updateIcon();
    updateTooltip();

    kdDebug() << "kiko Applet constructed" << endl;
}

KikoApplet::~KikoApplet() {
    if (m_process->state() != KikoProcess::Stopped) {
        m_process->stop();
    }
}

int KikoApplet::widthForHeight(int h) {
    return h;  // square applet
}

int KikoApplet::heightForWidth(int w) {
    return w;
}

void KikoApplet::mousePressEvent(TQMouseEvent* e) {
    if (e->button() == TQMouseEvent::RightButton) {
        m_menu->exec(e->globalPos());
        e->accept();
    } else {
        KPanelApplet::mousePressEvent(e);
    }
}

void KikoApplet::createMenu() {
    m_menu->clear();

    m_menu->insertItem(i18n("Start"), this, TQ_SLOT(onStart()));
    m_menu->insertItem(i18n("Stop"), this, TQ_SLOT(onStop()));
    m_menu->insertItem(i18n("Restart"), this, TQ_SLOT(onRestart()));
    m_menu->insertItem(i18n("Kill"), this, TQ_SLOT(onKill()));
    m_menu->insertSeparator();
    m_menu->insertItem(i18n("Show Logs"), this, TQ_SLOT(onShowLogs()));
    m_menu->insertSeparator();
    m_menu->insertItem(i18n("Configure..."), this, TQ_SLOT(configure()));
    // No "Quit" - applets shouldn't kill the panel
}

void KikoApplet::setupConfig() {
    TDEConfig* config = TDEGlobal::config();
    config->setGroup("KikoApplet");

    m_configPath = config->readPathEntry("ConfigPath", "");
    m_binaryPath = config->readEntry("BinaryPath", "kiko");
    m_port = config->readNumEntry("Port", 4000);

    // Default config path if empty
    if (m_configPath.isEmpty()) {
        TQString home = getenv("HOME");
        m_configPath = home + "/Documents/config-kiko.yaml";
    }
}

void KikoApplet::configure() {
    // Simple config dialog using KMessageBox + KFileDialog
    TQString newConfig = KFileDialog::getOpenFileName(
        m_configPath, "*.yaml", this,
        i18n("Select kiko Config File")
    );

    if (!newConfig.isEmpty()) {
        m_configPath = newConfig;
        m_process->setConfigPath(m_configPath);

        TDEConfig* config = TDEGlobal::config();
        config->setGroup("KikoApplet");
        config->writePathEntry("ConfigPath", m_configPath);
        config->sync();
    }
}

TQString KikoApplet::configFilePath() const {
    return m_configPath;
}

void KikoApplet::onStart() {
    if (m_process->state() == KikoProcess::Stopped) {
        m_process->start();
    }
}

void KikoApplet::onStop() {
    if (m_process->state() != KikoProcess::Stopped &&
        m_process->state() != KikoProcess::Stopping) {
        m_process->stop();
    }
}

void KikoApplet::onRestart() {
    m_process->restart();
}

void KikoApplet::onKill() {
    m_process->killProcess();
}

void KikoApplet::onShowLogs() {
    if (!m_logPanel) {
        m_logPanel = new KikoLogPanel(0, m_configPath.isEmpty() ? "~/.kiko/kiko.db" : m_configPath);
        m_logPanel->setCaption(i18n("kiko Proxy Logs"));
        m_logPanel->resize(800, 500);
    }
    m_logPanel->refresh();
    m_logPanel->show();
    m_logPanel->raise();
}

void KikoApplet::onProcessStateChanged(int state) {
    KikoProcess::State s = static_cast<KikoProcess::State>(state);
    updateIcon();
    updateTooltip();

    // Update menu item enabled states
    bool isRunning = (s == KikoProcess::Running);
    bool isStopped = (s == KikoProcess::Stopped);
    bool isBusy = (s == KikoProcess::Starting || s == KikoProcess::Stopping);

    m_menu->setItemEnabled(0, !isRunning && !isBusy);  // Start
    m_menu->setItemEnabled(1, isRunning && !isBusy);   // Stop
    m_menu->setItemEnabled(2, isRunning && !isBusy);   // Restart
}

void KikoApplet::onProcessOutput(const TQString& line) {
    kdDebug() << "kiko:" << line << endl;
    m_logBuffer.append(line);
    // Keep buffer reasonable size
    if (m_logBuffer.count() > 1000) {
        m_logBuffer.pop_front();
    }
}

void KikoApplet::onProcessError(const TQString& msg) {
    KMessageBox::error(this, msg, i18n("kiko Error"));
}

void KikoApplet::updateIcon() {
    TQPixmap pix;
    switch (m_process->state()) {
        case KikoProcess::Running:
            pix = m_iconRunning.pixmap(TQIconSet::Small, TQIconSet::Normal);
            break;
        case KikoProcess::Starting:
            pix = m_iconStarting.pixmap(TQIconSet::Small, TQIconSet::Normal);
            break;
        case KikoProcess::Error:
            pix = m_iconError.pixmap(TQIconSet::Small, TQIconSet::Normal);
            break;
        case KikoProcess::Stopping:
            pix = m_iconIdle.pixmap(TQIconSet::Small, TQIconSet::Normal);
            break;
        default:
            pix = m_iconStopped.pixmap(TQIconSet::Small, TQIconSet::Normal);
            break;
    }
    m_iconLabel->setPixmap(pix);
}

void KikoApplet::updateTooltip() {
    TQString tooltip;
    switch (m_process->state()) {
        case KikoProcess::Running:
            tooltip = i18n("kiko Proxy: Running on port %1").arg(m_port);
            break;
        case KikoProcess::Starting:
            tooltip = i18n("kiko Proxy: Starting...");
            break;
        case KikoProcess::Stopping:
            tooltip = i18n("kiko Proxy: Stopping...");
            break;
        case KikoProcess::Error:
            tooltip = i18n("kiko Proxy: Error");
            break;
        default:
            tooltip = i18n("kiko Proxy: Stopped");
            break;
    }
    TQToolTip::add(this, tooltip);
}