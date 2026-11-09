// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Nemotron 3 Ultra
/*
 * kiko-tqt3 Log Panel - Billing/Usage Log Viewer
 */

#include "kiko_log_panel.h"
#include <kdebug.h>
#include <tqlayout.h>
#include <tqheader.h>
#include <tqmessagebox.h>
#include <tqapplication.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>

KikoLogPanel::KikoLogPanel(TQWidget* parent, const TQString& dbPath)
    : TQWidget(parent)
    , m_db(nullptr)
    , m_currentPage(1)
    , m_pageSize(20)
    , m_totalEntries(0)
    , m_dbPath(dbPath)
{
    // Expand ~ in path
    if (m_dbPath.startsWith("~")) {
        const char* home = getenv("HOME");
        if (home) {
            m_dbPath = TQString(home) + m_dbPath.mid(1);
        }
    }

    setupUI();
    openDatabase();
    loadPage();

    // Auto-refresh timer (disabled by default)
    m_autoRefreshTimer = new TQTimer(this);
    connect(m_autoRefreshTimer, TQ_SIGNAL(timeout()), this, TQ_SLOT(onAutoRefresh()));
}

KikoLogPanel::~KikoLogPanel()
{
    closeDatabase();
}

void KikoLogPanel::setupUI()
{
    // Main layout
    TQVBoxLayout* mainLayout = new TQVBoxLayout(this);
    mainLayout->setMargin(4);
    mainLayout->setSpacing(4);

    // Table
    m_table = new TQTable(this);
    m_table->setNumCols(NUM_COLUMNS);
    m_table->setShowGrid(true);
    m_table->setSelectionMode(TQTable::SingleRow);
    m_table->setColumnMovingEnabled(false);
    m_table->setRowMovingEnabled(false);
    m_table->setSorting(true);
    m_table->verticalHeader()->hide();

    // Set column headers
    TQStringList headers;
    headers << "Timestamp" << "Model" << "Route" << "Tokens In" << "Tokens Out"
            << "Cost ($)" << "Latency (ms)" << "Attempts" << "Upstream" << "Error";
    TQHeader* hdr = m_table->horizontalHeader();
    if (hdr) {
        for (int i = 0; i < headers.count(); ++i) {
            hdr->setLabel(i, headers[i]);
        }
    }

    // Set column widths
    m_table->setColumnWidth(COL_TIMESTAMP, 140);
    m_table->setColumnWidth(COL_MODEL, 120);
    m_table->setColumnWidth(COL_ROUTE, 80);
    m_table->setColumnWidth(COL_TOKENS_IN, 70);
    m_table->setColumnWidth(COL_TOKENS_OUT, 70);
    m_table->setColumnWidth(COL_COST, 80);
    m_table->setColumnWidth(COL_LATENCY, 80);
    m_table->setColumnWidth(COL_ATTEMPTS, 60);
    m_table->setColumnWidth(COL_UPSTREAM, 100);
    m_table->setColumnWidth(COL_ERROR, 80);

    mainLayout->addWidget(m_table, 1);  // Stretch factor 1

    // Pagination controls
    TQHBoxLayout* paginationLayout = new TQHBoxLayout();
    paginationLayout->setSpacing(8);

    m_btnPrev = new TQPushButton("← Prev", this);
    m_btnPrev->setFixedWidth(70);
    connect(m_btnPrev, TQ_SIGNAL(clicked()), this, TQ_SLOT(onPrevPage()));

    m_pageInfoLabel = new TQLabel("Showing 0–0 of 0 entries", this);

    m_pageIndicatorLabel = new TQLabel("Page 1 of 1", this);

    m_btnNext = new TQPushButton("Next →", this);
    m_btnNext->setFixedWidth(70);
    connect(m_btnNext, TQ_SIGNAL(clicked()), this, TQ_SLOT(onNextPage()));

    // Page size selector
    TQLabel* pageSizeLabel = new TQLabel("Per page:", this);
    m_pageSizeCombo = new TQComboBox(this);
    m_pageSizeCombo->insertItem("10");
    m_pageSizeCombo->insertItem("20");
    m_pageSizeCombo->insertItem("30");
    m_pageSizeCombo->insertItem("50");
    m_pageSizeCombo->insertItem("100");
    m_pageSizeCombo->setCurrentText(TQString::number(m_pageSize));
    connect(m_pageSizeCombo, TQ_SIGNAL(activated(int)), this, TQ_SLOT(onPageSizeChanged(int)));

    // Refresh button
    m_btnRefresh = new TQPushButton("Refresh", this);
    m_btnRefresh->setFixedWidth(70);
    connect(m_btnRefresh, TQ_SIGNAL(clicked()), this, TQ_SLOT(refresh()));

    // Auto-refresh checkbox
    m_chkAutoRefresh = new TQCheckBox("Auto-refresh (5s)", this);
    connect(m_chkAutoRefresh, TQ_SIGNAL(toggled(bool)), this, TQ_SLOT(onAutoRefresh()));

    paginationLayout->addWidget(m_btnPrev);
    paginationLayout->addWidget(m_pageInfoLabel);
    paginationLayout->addWidget(m_pageIndicatorLabel);
    paginationLayout->addWidget(m_btnNext);
    paginationLayout->addStretch(1);
    paginationLayout->addWidget(pageSizeLabel);
    paginationLayout->addWidget(m_pageSizeCombo);
    paginationLayout->addWidget(m_btnRefresh);
    paginationLayout->addWidget(m_chkAutoRefresh);

    mainLayout->addLayout(paginationLayout);

    updatePaginationUI();
}

void KikoLogPanel::openDatabase()
{
    if (m_db) return;

    int rc = sqlite3_open(m_dbPath.latin1(), &m_db);
    if (rc != SQLITE_OK) {
        kdDebug() << "Failed to open database: " << sqlite3_errmsg(m_db) << endl;
        if (m_db) {
            sqlite3_close(m_db);
            m_db = nullptr;
        }
    } else {
        // Enable WAL mode for better concurrency
        sqlite3_exec(m_db, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
        sqlite3_exec(m_db, "PRAGMA synchronous=NORMAL", nullptr, nullptr, nullptr);
        sqlite3_exec(m_db, "PRAGMA busy_timeout=5000", nullptr, nullptr, nullptr);
    }
}

void KikoLogPanel::closeDatabase()
{
    if (m_db) {
        sqlite3_close(m_db);
        m_db = nullptr;
    }
}

void KikoLogPanel::refresh()
{
    m_currentPage = 1;
    loadPage();
}

void KikoLogPanel::setPageSize(int size)
{
    if (size > 0 && size != m_pageSize) {
        m_pageSize = size;
        m_currentPage = 1;
        loadPage();
    }
}

void KikoLogPanel::onPrevPage()
{
    if (m_currentPage > 1) {
        m_currentPage--;
        loadPage();
    }
}

void KikoLogPanel::onNextPage()
{
    int totalPages = (m_totalEntries + m_pageSize - 1) / m_pageSize;
    if (m_currentPage < totalPages) {
        m_currentPage++;
        loadPage();
    }
}

void KikoLogPanel::onPageSizeChanged(int index)
{
    TQString text = m_pageSizeCombo->text(index);
    bool ok = false;
    int size = text.toInt(&ok);
    if (ok && size > 0) {
        m_pageSize = size;
        m_currentPage = 1;
        loadPage();
    }
}

void KikoLogPanel::onAutoRefresh()
{
    if (m_chkAutoRefresh->isChecked()) {
        m_autoRefreshTimer->start(5000);  // 5 seconds
        loadPage();
    } else {
        m_autoRefreshTimer->stop();
    }
}

void KikoLogPanel::loadPage()
{
    if (!m_db) {
        openDatabase();
        if (!m_db) return;
    }

    m_totalEntries = queryTotalCount();
    m_currentPageData = queryLogs(m_pageSize, (m_currentPage - 1) * m_pageSize);
    updateTable();
    updatePaginationUI();
}

int KikoLogPanel::queryTotalCount()
{
    if (!m_db) return 0;

    sqlite3_stmt* stmt;
    const char* sql = "SELECT COUNT(*) FROM billing";
    int count = 0;

    if (sqlite3_prepare_v2(m_db, sql, -1, &stmt, nullptr) == SQLITE_OK) {
        if (sqlite3_step(stmt) == SQLITE_ROW) {
            count = sqlite3_column_int(stmt, 0);
        }
        sqlite3_finalize(stmt);
    }
    return count;
}

std::vector<LogEntry> KikoLogPanel::queryLogs(int limit, int offset)
{
    std::vector<LogEntry> results;
    if (!m_db) return results;

    sqlite3_stmt* stmt;
    const char* sql = "SELECT id, model, tokens_in, tokens_out, cost_usd, ts, route, "
                      "attempts, latency_ms, upstream_model, error "
                      "FROM billing ORDER BY ts DESC LIMIT ? OFFSET ?";

    if (sqlite3_prepare_v2(m_db, sql, -1, &stmt, nullptr) == SQLITE_OK) {
        sqlite3_bind_int(stmt, 1, limit);
        sqlite3_bind_int(stmt, 2, offset);

        while (sqlite3_step(stmt) == SQLITE_ROW) {
            LogEntry entry;
            entry.id = sqlite3_column_int(stmt, 0);
            entry.model = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1)) ?
                          reinterpret_cast<const char*>(sqlite3_column_text(stmt, 1)) : "";
            entry.tokens_in = sqlite3_column_int(stmt, 2);
            entry.tokens_out = sqlite3_column_int(stmt, 3);
            entry.cost_usd = sqlite3_column_double(stmt, 4);
            entry.ts = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 5)) ?
                       reinterpret_cast<const char*>(sqlite3_column_text(stmt, 5)) : "";
            entry.route = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 6)) ?
                          reinterpret_cast<const char*>(sqlite3_column_text(stmt, 6)) : "";
            entry.attempts = sqlite3_column_int(stmt, 7);
            entry.latency_ms = sqlite3_column_int(stmt, 8);
            entry.upstream_model = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 9)) ?
                                   reinterpret_cast<const char*>(sqlite3_column_text(stmt, 9)) : "";
            entry.error = reinterpret_cast<const char*>(sqlite3_column_text(stmt, 10)) ?
                          reinterpret_cast<const char*>(sqlite3_column_text(stmt, 10)) : "";
            results.push_back(entry);
        }
        sqlite3_finalize(stmt);
    }
    return results;
}

void KikoLogPanel::updateTable()
{
    m_table->setNumRows(m_currentPageData.size());

    for (size_t row = 0; row < m_currentPageData.size(); ++row) {
        const LogEntry& e = m_currentPageData[row];

        // Timestamp
        TQTableItem* item = new TQTableItem(m_table, TQTableItem::Never, formatTimestamp(e.ts));
        m_table->setItem(row, COL_TIMESTAMP, item);

        // Model
        item = new TQTableItem(m_table, TQTableItem::Never, TQString::fromUtf8(e.model.c_str()));
        m_table->setItem(row, COL_MODEL, item);

        // Route
        item = new TQTableItem(m_table, TQTableItem::Never, TQString::fromUtf8(e.route.c_str()));
        m_table->setItem(row, COL_ROUTE, item);

        // Tokens In
        item = new TQTableItem(m_table, TQTableItem::Never, formatNumber(e.tokens_in));
        m_table->setItem(row, COL_TOKENS_IN, item);

        // Tokens Out
        item = new TQTableItem(m_table, TQTableItem::Never, formatNumber(e.tokens_out));
        m_table->setItem(row, COL_TOKENS_OUT, item);

        // Cost
        item = new TQTableItem(m_table, TQTableItem::Never, formatCost(e.cost_usd));
        m_table->setItem(row, COL_COST, item);

        // Latency
        TQString latencyStr = (e.latency_ms > 0) ? formatNumber(e.latency_ms) : "-";
        item = new TQTableItem(m_table, TQTableItem::Never, latencyStr);
        m_table->setItem(row, COL_LATENCY, item);

        // Attempts
        TQString attemptsStr = (e.attempts > 0) ? formatNumber(e.attempts) : "-";
        item = new TQTableItem(m_table, TQTableItem::Never, attemptsStr);
        m_table->setItem(row, COL_ATTEMPTS, item);

        // Upstream Model
        item = new TQTableItem(m_table, TQTableItem::Never, TQString::fromUtf8(e.upstream_model.c_str()));
        m_table->setItem(row, COL_UPSTREAM, item);

        // Error
        TQString errorStr = e.error.empty() ? "-" : TQString::fromUtf8(e.error.c_str());
        item = new TQTableItem(m_table, TQTableItem::Never, errorStr);
        m_table->setItem(row, COL_ERROR, item);
    }
}

void KikoLogPanel::updatePaginationUI()
{
    int totalPages = (m_totalEntries + m_pageSize - 1) / m_pageSize;
    if (totalPages == 0) totalPages = 1;

    int start = (m_totalEntries > 0) ? (m_currentPage - 1) * m_pageSize + 1 : 0;
    int end = std::min(m_currentPage * m_pageSize, m_totalEntries);

    m_pageInfoLabel->setText(TQString("Showing %1–%2 of %3 entries").arg(start).arg(end).arg(m_totalEntries));
    m_pageIndicatorLabel->setText(TQString("Page %1 of %2").arg(m_currentPage).arg(totalPages));

    m_btnPrev->setEnabled(m_currentPage > 1);
    m_btnNext->setEnabled(m_currentPage < totalPages);
}

TQString KikoLogPanel::formatTimestamp(const std::string& ts)
{
    if (ts.empty()) return "-";

    // Parse ISO timestamp (e.g., "2026-10-31T17:24:30.123456")
    TQDateTime dt = TQDateTime::fromString(TQString::fromUtf8(ts.c_str()), TQt::ISODate);
    if (dt.isValid()) {
        return dt.toString("yyyy-MM-dd hh:mm:ss");
    }

    // Fallback - try to extract date/time manually
    if (ts.length() >= 19) {
        return TQString::fromUtf8(ts.substr(0, 19).c_str()).replace('T', ' ');
    }
    return TQString::fromUtf8(ts.c_str());
}

TQString KikoLogPanel::formatCost(double cost)
{
    if (cost <= 0) return "-";
    char buf[32];
    snprintf(buf, sizeof(buf), "$%.6f", cost);
    return TQString(buf);
}

TQString KikoLogPanel::formatNumber(int num)
{
    if (num <= 0) return "0";
    char buf[32];
    snprintf(buf, sizeof(buf), "%d", num);
    return TQString(buf);
}

