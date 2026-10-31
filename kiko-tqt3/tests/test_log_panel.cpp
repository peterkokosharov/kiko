/*
 * kiko-tqt3 Log Panel Test Application
 * Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
 * LLM: gemini-flash-lite
 * Licensed under MIT License
 */

#include <tqapplication.h>
#include <tqmainwindow.h>
#include "../src/kiko_log_panel.h"

int main(int argc, char **argv) {
    TQApplication app(argc, argv);

    QMainWindow mainWindow;
    mainWindow.setCaption("kiko-tqt3 Log Panel Test");
    mainWindow.resize(900, 500);

    // Create log panel as central widget
    KikoLogPanel *logPanel = new KikoLogPanel(&mainWindow, "~/.kiko/kiko.db");
    mainWindow.setCentralWidget(logPanel);

    mainWindow.show();
    app.setMainWidget(&mainWindow);

    return app.exec();
}
