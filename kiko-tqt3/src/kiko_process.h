/*
 * kiko-tqt3 Process Management - TQObject wrapper for TDE applet
 * Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
 * Licensed under MIT License
 */

#ifndef KIKO_PROCESS_H
#define KIKO_PROCESS_H

#include <tdemacros.h>
#include <tqobject.h>
#include <tqstring.h>

class TDE_EXPORT KikoProcess : public TQObject
{
    TQ_OBJECT

public:
    enum State {
        Stopped,
        Starting,
        Running,
        Stopping,
        Error
    };
    TQ_ENUMS(State)

    explicit KikoProcess(TQObject* parent = 0);
    ~KikoProcess() override;

    State state() const { return m_state; }
    TQString serviceName() const { return m_serviceName; }

    void setServiceName(const TQString& name);
    void setConfigPath(const TQString& path);
    void setPort(int port);
    void setKikoBinary(const TQString& binary);

    // Async operations via systemctl --user
    void start();
    void stop();
    void restart();
    void killProcess();

signals:
    void stateChanged(int state);
    void outputReceived(const TQString& line);
    void errorOccurred(const TQString& msg);

private slots:
    void checkServiceStatus();

private:
    void setState(State newState);
    bool runSystemctl(const char* action, TQString& errorMsg);

    TQString m_serviceName;
    TQString m_configPath;
    TQString m_binaryPath;
    int m_port;
    State m_state;
};

#endif // KIKO_PROCESS_H