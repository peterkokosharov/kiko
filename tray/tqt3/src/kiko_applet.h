// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko TDE Panel Applet
 */

#ifndef KIKO_APPLET_H
#define KIKO_APPLET_H

#include <kpanelapplet.h>
#include <tdemacros.h>
#include <tqwidget.h>
#include <tqiconset.h>
#include <tqpopupmenu.h>
#include <tqlabel.h>
#include <tqtimer.h>
#include <tqevent.h>
#include <tqstringlist.h>
#include "kiko_process.h"

class KikoLogPanel;

class TDE_EXPORT KikoApplet : public KPanelApplet {
    TQ_OBJECT

public:
    KikoApplet(const TQString& configFile, TQWidget* parent);
    ~KikoApplet();

    // KPanelApplet virtuals
    int widthForHeight(int h);
    int heightForWidth(int w);
    bool hasConfiguration() { return true; }

protected:
    void mousePressEvent(TQMouseEvent* e);

private slots:
    void onStart();
    void onStop();
    void onRestart();
    void onKill();
    void onShowLogs();
    void configure();  // Must be slot for menu connection
    void onProcessStateChanged(int state);
    void onProcessOutput(const TQString& line);
    void onProcessError(const TQString& msg);
    void updateIcon();
    void updateTooltip();

private:
    void createMenu();
    void setupConfig();
    TQString configFilePath() const;

    KikoProcess* m_process;
    TQPopupMenu* m_menu;
    TQTimer* m_tooltipTimer;

    // Icons for different states (TQt3 uses TQIconSet)
    TQIconSet m_iconIdle;
    TQIconSet m_iconRunning;
    TQIconSet m_iconError;
    TQIconSet m_iconStopped;
    TQIconSet m_iconStarting;

    // UI
    TQLabel* m_iconLabel;

    // Log panel (billing/usage table)
    KikoLogPanel* m_logPanel = nullptr;

    // Config
    TQStringList m_logBuffer;
    TQString m_configPath;
    TQString m_binaryPath;
    int m_port;
};

#endif // KIKO_APPLET_H