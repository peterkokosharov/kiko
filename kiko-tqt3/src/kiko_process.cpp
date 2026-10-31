/*
 * kiko-tqt3 Process Management - TQObject wrapper for TDE applet
 * Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
 * Licensed under MIT License
 */

#include "kiko_process.h"
#include <kdebug.h>
#include <tqprocess.h>
#include <tqtimer.h>
#include <cstdio>

KikoProcess::KikoProcess(TQObject* parent)
    : TQObject(parent)
    , m_serviceName("kiko")
    , m_configPath("")
    , m_binaryPath("kiko")
    , m_port(4002)  // from ~/.kiko/kiko.toml
    , m_state(Stopped)
{
    // Check service status every 2 seconds (like litellm-try)
    TQTimer* checkTimer = new TQTimer(this);
    connect(checkTimer, TQ_SIGNAL(timeout()), this, TQ_SLOT(checkServiceStatus()));
    checkTimer->start(2000);

    // Initial status check
    checkServiceStatus();
}

KikoProcess::~KikoProcess()
{
    // Don't stop the service on applet destruction - it's a system service
}

void KikoProcess::setServiceName(const TQString& name)
{
    m_serviceName = name;
}

void KikoProcess::setConfigPath(const TQString& path)
{
    m_configPath = path;
}

void KikoProcess::setPort(int port)
{
    m_port = port;
}

void KikoProcess::setKikoBinary(const TQString& binary)
{
    m_binaryPath = binary;
}

void KikoProcess::setState(State newState)
{
    if (m_state != newState) {
        m_state = newState;
        emit stateChanged(static_cast<int>(newState));
    }
}

bool KikoProcess::runSystemctl(const char* action, TQString& errorMsg)
{
    // Use synchronous systemctl call
    FILE* pipe = popen(TQString("systemctl --user %1 %2 2>&1").arg(action).arg(m_serviceName).latin1(), "r");
    if (pipe) {
        char buffer[256];
        TQString output;
        while (fgets(buffer, sizeof(buffer), pipe)) {
            output += TQString::fromLatin1(buffer);
        }
        int status = pclose(pipe);
        if (status == 0) {
            return true;
        } else {
            errorMsg = output.stripWhiteSpace();
            if (errorMsg.isEmpty()) {
                errorMsg = TQString("systemctl %1 failed with status %2").arg(action).arg(status);
            }
            return false;
        }
    }
    errorMsg = "Failed to run systemctl";
    return false;
}

void KikoProcess::start()
{
    if (m_state == Running || m_state == Starting) {
        return;
    }

    setState(Starting);
    kdDebug() << "Starting kiko via systemctl" << endl;

    TQString errorMsg;
    if (runSystemctl("start", errorMsg)) {
        // Wait a bit then check status
        TQTimer::singleShot(1000, this, TQ_SLOT(checkServiceStatus()));
    } else {
        setState(Error);
        emit errorOccurred(errorMsg);
    }
}

void KikoProcess::stop()
{
    if (m_state == Stopped || m_state == Stopping) {
        return;
    }

    setState(Stopping);
    kdDebug() << "Stopping kiko via systemctl" << endl;

    TQString errorMsg;
    if (runSystemctl("stop", errorMsg)) {
        TQTimer::singleShot(1000, this, TQ_SLOT(checkServiceStatus()));
    } else {
        setState(Error);
        emit errorOccurred(errorMsg);
    }
}

void KikoProcess::restart()
{
    if (m_state == Stopped) {
        start();
        return;
    }

    setState(Stopping);
    kdDebug() << "Restarting kiko via systemctl" << endl;

    TQString errorMsg;
    if (runSystemctl("restart", errorMsg)) {
        TQTimer::singleShot(1500, this, TQ_SLOT(checkServiceStatus()));
    } else {
        setState(Error);
        emit errorOccurred(errorMsg);
    }
}

void KikoProcess::killProcess()
{
    if (m_state == Stopped || m_state == Stopping) {
        return;
    }

    setState(Stopping);
    kdDebug() << "Killing kiko via systemctl" << endl;

    TQString errorMsg;
    if (runSystemctl("kill", errorMsg)) {
        TQTimer::singleShot(1000, this, TQ_SLOT(checkServiceStatus()));
    } else {
        setState(Error);
        emit errorOccurred(errorMsg);
    }
}

void KikoProcess::checkServiceStatus()
{
    // Use synchronous shell command to check status (like litellm-try)
    FILE* pipe = popen("systemctl --user is-active kiko 2>/dev/null", "r");
    if (pipe) {
        char buffer[128];
        if (fgets(buffer, sizeof(buffer), pipe)) {
            TQString output = TQString::fromLatin1(buffer).stripWhiteSpace();
            kdDebug() << "Service status:" << output << endl;

            if (output == "active") {
                setState(Running);
            } else if (output == "inactive" || output == "dead") {
                setState(Stopped);
            } else if (output == "activating" || output == "starting") {
                setState(Starting);
            } else if (output == "deactivating" || output == "stopping") {
                setState(Stopping);
            } else if (output == "failed") {
                setState(Error);
            } else {
                setState(Stopped);
            }
        }
        pclose(pipe);
    } else {
        kdDebug() << "Failed to check service status" << endl;
        setState(Error);
    }
}