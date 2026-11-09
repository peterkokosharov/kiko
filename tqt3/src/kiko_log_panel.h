// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko-tqt3 Log Panel - Billing/Usage Log Viewer
 */

#ifndef KIKO_LOG_PANEL_H
#define KIKO_LOG_PANEL_H

#include <tdemacros.h>
#include <tqwidget.h>
#include <tqtable.h>
#include <tqpushbutton.h>
#include <tqcombobox.h>
#include <tqcheckbox.h>
#include <tqlabel.h>
#include <tqlayout.h>
#include <tqheader.h>
#include <tqtimer.h>
#include <tqdatetime.h>
#include <sqlite3.h>
#include <vector>
#include <string>

struct LogEntry {
    int id;
    std::string model;
    std::string route;
    int tokens_in;
    int tokens_out;
    double cost_usd;
    std::string ts;
    int latency_ms;
    int attempts;
    std::string upstream_model;
    std::string error;
};

class TDE_EXPORT KikoLogPanel : public TQWidget
{
    TQ_OBJECT

public:
    explicit KikoLogPanel(TQWidget* parent = 0, const TQString& dbPath = "~/.kiko/kiko.db");
    ~KikoLogPanel() override;

    // Public API
    void refresh();
    void setPageSize(int size);

private slots:
    void onPrevPage();
    void onNextPage();
    void onPageSizeChanged(int index);
    void onAutoRefresh();

private:
    void setupUI();
    void loadPage();
    void updateTable();
    void updatePaginationUI();
    void openDatabase();
    void closeDatabase();
    std::vector<LogEntry> queryLogs(int limit, int offset);
    int queryTotalCount();
    TQString formatTimestamp(const std::string& ts);
    TQString formatCost(double cost);
    TQString formatNumber(int num);

    // UI Components
    TQTable* m_table;
    TQPushButton* m_btnPrev;
    TQPushButton* m_btnNext;
    TQComboBox* m_pageSizeCombo;
    TQLabel* m_pageInfoLabel;
    TQLabel* m_pageIndicatorLabel;
    TQPushButton* m_btnRefresh;
    TQCheckBox* m_chkAutoRefresh;
    TQTimer* m_autoRefreshTimer;

    // Data
    sqlite3* m_db;
    std::vector<LogEntry> m_currentPageData;
    int m_currentPage;
    int m_pageSize;
    int m_totalEntries;
    TQString m_dbPath;

    // Column indices
    enum Columns {
        COL_TIMESTAMP = 0,
        COL_MODEL = 1,
        COL_ROUTE = 2,
        COL_TOKENS_IN = 3,
        COL_TOKENS_OUT = 4,
        COL_COST = 5,
        COL_LATENCY = 6,
        COL_ATTEMPTS = 7,
        COL_UPSTREAM = 8,
        COL_ERROR = 9,
        NUM_COLUMNS = 10
    };
};

#endif // KIKO_LOG_PANEL_H