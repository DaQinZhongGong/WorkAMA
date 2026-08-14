#!/usr/bin/env python3
"""R184: 创建 P2-02 文档报告生成 docreport 包的全部 Go 文件。
参考 R183 opt 包的加法式落地模式。
"""
import os

PKG_DIR = '/workspace/backend-go/services/governance/internal/docreport'
os.makedirs(PKG_DIR, exist_ok=True)

# ============ 1. model.go ============
MODEL_GO = '''package docreport

import (
	"encoding/json"
	"errors"
	"time"
)

// ErrInvalidInput 入参非法（映射为 400）。
var ErrInvalidInput = errors.New("docreport: invalid input")

// ErrNotFound 资源不存在（映射为 404）。
var ErrNotFound = errors.New("docreport: not found")

// ---- 报告项目（report_project）----

// ReportProject 文档报告项目（P2-02 设计 19 §8：report_project）。
type ReportProject struct {
	ID          int64           `json:"id"`
	TenantID    int64           `json:"tenantId"`
	Title       string          `json:"title"`
	DocType     string          `json:"docType"`     // ppt | word | pdf | markdown
	Topic       string          `json:"topic"`       // 主题/需求描述
	Audience    string          `json:"audience"`    // 受众
	Status      string          `json:"status"`      // draft | generating | done | failed
	Outline     json.RawMessage `json:"outline"`     // []ReportSection JSON
	TemplateID  int64           `json:"templateId"`  // 关联模板（0 表示默认）
	ArtifactKey string          `json:"artifactKey"` // 生成产物在 MinIO 的 key
	CreatedBy   int64           `json:"createdBy"`
	CreatedAt   time.Time       `json:"createdAt"`
	UpdatedAt   time.Time       `json:"updatedAt"`
}

// ReportSection 报告章节（P2-02 设计 19 §8：report_section）。
type ReportSection struct {
	ID        int64  `json:"id"`
	ProjectID int64  `json:"projectId"`
	Title     string `json:"title"`
	Content   string `json:"content"`   // 正文（Markdown）
	OrderIdx  int    `json:"orderIdx"`  // 排序
	Status    string `json:"status"`    // pending | written | reviewed
	CreatedAt time.Time `json:"createdAt"`
	UpdatedAt time.Time `json:"updatedAt"`
}

// ReportGenerationRun 一次生成运行（P2-02 设计 19 §8：report_generation_run）。
type ReportGenerationRun struct {
	ID            int64           `json:"id"`
	ProjectID     int64           `json:"projectId"`
	TenantID      int64           `json:"tenantId"`
	Stage         string          `json:"stage"`        // plan | retrieve | write | design | review | render | done | failed
	StageProgress json.RawMessage `json:"stageProgress"` // {stage: status} JSON
	AgentLog      json.RawMessage `json:"agentLog"`      // []AgentStep JSON
	OutputType    string          `json:"outputType"`    // ppt | word | pdf | markdown
	ArtifactKey   string          `json:"artifactKey"`   // 产物 key
	CharCount     int             `json:"charCount"`     // 生成字符数
	DurationMS    int64           `json:"durationMs"`
	Status        string          `json:"status"`        // running | done | failed
	ErrorMessage   string          `json:"errorMessage"`
	CreatedAt     time.Time       `json:"createdAt"`
	FinishedAt    time.Time       `json:"finishedAt"`
}

// ReportTemplate 报告模板（P2-02 设计 19 §9：模板版本化）。
type ReportTemplate struct {
	ID          int64           `json:"id"`
	TenantID    int64           `json:"tenantId"`
	Name        string          `json:"name"`
	DocType     string          `json:"docType"`     // ppt | word | pdf | markdown
	Version     int              `json:"version"`     // 模板版本（锁定可复现）
	Layout      json.RawMessage `json:"layout"`      // 模板布局 JSON（章节占位、字体、配色）
	IsBuiltin   bool            `json:"isBuiltin"`   // 是否内置
	CreatedAt   time.Time       `json:"createdAt"`
}

// AgentStep 单个 Agent 的执行记录（P2-02 设计 19 §5：6 Agent 分工）。
type AgentStep struct {
	Agent   string `json:"agent"`   // plan | retrieve | write | design | review | render
	Stage   string `json:"stage"`
	Status  string `json:"status"`  // start | done | error
	Message string `json:"message"`
	Tokens  int    `json:"tokens"`  // mock token 用量
	DurationMS int64 `json:"durationMs"`
}

// ReportArtifact 生成产物（渲染后的文档内容）。
type ReportArtifact struct {
	DocType    string            `json:"docType"`
	FileName   string            `json:"fileName"`
	Content    string            `json:"content"`    // markdown 直出 / ppt|word|pdf 为 JSON 字符串
	MimeType   string            `json:"mimeType"`
	Size       int               `json:"size"`       // 字节数
	Sections   []ReportSection   `json:"sections"`   // 章节明细
	CharCount  int               `json:"charCount"`
	ArtifactKey string           `json:"artifactKey"` // MinIO key（mock：docreport/{projectId}/{runId}.{ext}）
}

// ---- 入参 ----

// CreateProjectInput 创建报告项目入参。
type CreateProjectInput struct {
	TenantID   int64  `json:"tenantId"`
	Title      string `json:"title"`
	DocType    string `json:"docType"`
	Topic      string `json:"topic"`
	Audience   string `json:"audience"`
	TemplateID int64  `json:"templateId"`
	CreatedBy  int64  `json:"createdBy"`
}

// UpdateOutlineInput 更新大纲入参。
type UpdateOutlineInput struct {
	TenantID int64            `json:"tenantId"`
	Sections []OutlineSection `json:"sections"`
}

// OutlineSection 大纲章节。
type OutlineSection struct {
	Title    string `json:"title"`
	OrderIdx int    `json:"orderIdx"`
}

// GenerateInput 生成报告入参。
type GenerateInput struct {
	TenantID   int64  `json:"tenantId"`
	OutputType string `json:"outputType"` // ppt | word | pdf | markdown（缺省用项目 docType）
	Force      bool   `json:"force"`      // 强制重新生成
}

// ExportInput 导出入参。
type ExportInput struct {
	TenantID   int64  `json:"tenantId"`
	OutputType string `json:"outputType"` // ppt | word | pdf | markdown
}

// CreateTemplateInput 创建模板入参。
type CreateTemplateInput struct {
	TenantID int64           `json:"tenantId"`
	Name     string          `json:"name"`
	DocType  string          `json:"docType"`
	Layout   json.RawMessage `json:"layout"`
}

// ---- 默认值 ----

// DefaultLayout 默认模板布局。
func DefaultLayout() map[string]any {
	return map[string]any{
		"font":      "Microsoft YaHei",
		"fontSize":  14,
		"color":     "#333333",
		"headingColor": "#1a73e8",
		"margin":    "2cm",
		"pageSize":  "A4",
		"sections":  []string{"引言", "正文", "结论"},
	}
}

// SupportedDocTypes 支持的文档类型。
var SupportedDocTypes = []string{"ppt", "word", "pdf", "markdown"}
'''

# ============ 2. repository.go ============
REPOSITORY_GO = '''package docreport

import "context"

// Repository 文档报告生成的存储抽象（P2-02 设计 19 §8）。
type Repository interface {
	// 报告项目
	CreateProject(ctx context.Context, p *ReportProject) error
	GetProject(ctx context.Context, id, tenantID int64) (*ReportProject, error)
	ListProjects(ctx context.Context, tenantID int64) ([]*ReportProject, error)
	UpdateProjectStatus(ctx context.Context, id int64, status, artifactKey string) error

	// 章节
	ListSections(ctx context.Context, projectID, tenantID int64) ([]*ReportSection, error)
	UpsertSections(ctx context.Context, projectID, tenantID int64, sections []*ReportSection) error

	// 生成运行
	CreateRun(ctx context.Context, r *ReportGenerationRun) error
	GetRun(ctx context.Context, id, tenantID int64) (*ReportGenerationRun, error)
	ListRuns(ctx context.Context, projectID, tenantID int64) ([]*ReportGenerationRun, error)
	UpdateRunStage(ctx context.Context, id int64, stage, status string, progress, agentLog json.RawMessage) error
	FinishRun(ctx context.Context, id int64, status, artifactKey string, charCount int, durationMS int64, errMsg string) error

	// 模板
	CreateTemplate(ctx context.Context, t *ReportTemplate) error
	GetTemplate(ctx context.Context, id, tenantID int64) (*ReportTemplate, error)
	ListTemplates(ctx context.Context, tenantID int64) ([]*ReportTemplate, error)

	Close() error
}
'''

# ============ 3. postgres_repository.go ============
POSTGRES_REPOSITORY_GO = '''package docreport

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type postgresRepository struct {
	pool *pgxpool.Pool
}

// OpenPostgresRepository 用数据库连接串构造仓储（与 gbi/opt 同构）。
func OpenPostgresRepository(ctx context.Context, databaseURL string) (Repository, error) {
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		return nil, fmt.Errorf("docreport: open pgxpool: %w", err)
	}
	return &postgresRepository{pool: pool}, nil
}

func (r *postgresRepository) Close() error {
	r.pool.Close()
	return nil
}

// ---- 项目 ----

func (r *postgresRepository) CreateProject(ctx context.Context, p *ReportProject) error {
	p.CreatedAt = time.Now()
	p.UpdatedAt = time.Now()
	if p.Status == "" {
		p.Status = "draft"
	}
	if len(p.Outline) == 0 {
		p.Outline = json.RawMessage("[]")
	}
	const q = `INSERT INTO report_project
		(tenant_id, title, doc_type, topic, audience, status, outline, template_id, artifact_key, created_by, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id`
	return r.pool.QueryRow(ctx, q,
		p.TenantID, p.Title, p.DocType, p.Topic, p.Audience, p.Status,
		string(p.Outline), p.TemplateID, p.ArtifactKey, p.CreatedBy, p.CreatedAt, p.UpdatedAt,
	).Scan(&p.ID)
}

func (r *postgresRepository) GetProject(ctx context.Context, id, tenantID int64) (*ReportProject, error) {
	const q = `SELECT id,tenant_id,title,doc_type,topic,audience,status,outline,template_id,artifact_key,created_by,created_at,updated_at
		FROM report_project WHERE id=$1 AND tenant_id=$2`
	p := &ReportProject{}
	var outline []byte
	err := r.pool.QueryRow(ctx, q, id, tenantID).Scan(
		&p.ID, &p.TenantID, &p.Title, &p.DocType, &p.Topic, &p.Audience, &p.Status,
		&outline, &p.TemplateID, &p.ArtifactKey, &p.CreatedBy, &p.CreatedAt, &p.UpdatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("%w: project %d", ErrNotFound, id)
	}
	if err != nil {
		return nil, err
	}
	p.Outline = json.RawMessage(outline)
	return p, nil
}

func (r *postgresRepository) ListProjects(ctx context.Context, tenantID int64) ([]*ReportProject, error) {
	const q = `SELECT id,tenant_id,title,doc_type,topic,audience,status,outline,template_id,artifact_key,created_by,created_at,updated_at
		FROM report_project WHERE tenant_id=$1 ORDER BY id DESC`
	rows, err := r.pool.Query(ctx, q, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ReportProject
	for rows.Next() {
		p := &ReportProject{}
		var outline []byte
		if err := rows.Scan(
			&p.ID, &p.TenantID, &p.Title, &p.DocType, &p.Topic, &p.Audience, &p.Status,
			&outline, &p.TemplateID, &p.ArtifactKey, &p.CreatedBy, &p.CreatedAt, &p.UpdatedAt,
		); err != nil {
			return nil, err
		}
		p.Outline = json.RawMessage(outline)
		out = append(out, p)
	}
	return out, rows.Err()
}

func (r *postgresRepository) UpdateProjectStatus(ctx context.Context, id int64, status, artifactKey string) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE report_project SET status=$2, artifact_key=$3, updated_at=now() WHERE id=$1`,
		id, status, artifactKey)
	return err
}

// ---- 章节 ----

func (r *postgresRepository) ListSections(ctx context.Context, projectID, tenantID int64) ([]*ReportSection, error) {
	const q = `SELECT id,project_id,title,content,order_idx,status,created_at,updated_at
		FROM report_section WHERE project_id=$1 AND tenant_id=$2 ORDER BY order_idx ASC`
	rows, err := r.pool.Query(ctx, q, projectID, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ReportSection
	for rows.Next() {
		s := &ReportSection{}
		if err := rows.Scan(&s.ID, &s.ProjectID, &s.Title, &s.Content, &s.OrderIdx, &s.Status, &s.CreatedAt, &s.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

func (r *postgresRepository) UpsertSections(ctx context.Context, projectID, tenantID int64, sections []*ReportSection) error {
	tx, err := r.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	// 先删除旧章节
	if _, err := tx.Exec(ctx, `DELETE FROM report_section WHERE project_id=$1`, projectID); err != nil {
		return err
	}
	for i, s := range sections {
		s.ProjectID = projectID
		s.OrderIdx = i
		s.CreatedAt = time.Now()
		s.UpdatedAt = time.Now()
		if s.Status == "" {
			s.Status = "pending"
		}
		const q = `INSERT INTO report_section (project_id, title, content, order_idx, status, created_at, updated_at)
			VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`
		if err := tx.QueryRow(ctx, q,
			s.ProjectID, s.Title, s.Content, s.OrderIdx, s.Status, s.CreatedAt, s.UpdatedAt,
		).Scan(&s.ID); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// ---- 生成运行 ----

func (r *postgresRepository) CreateRun(ctx context.Context, run *ReportGenerationRun) error {
	run.CreatedAt = time.Now()
	if run.Status == "" {
		run.Status = "running"
	}
	if run.Stage == "" {
		run.Stage = "plan"
	}
	if len(run.StageProgress) == 0 {
		run.StageProgress = json.RawMessage("{}")
	}
	if len(run.AgentLog) == 0 {
		run.AgentLog = json.RawMessage("[]")
	}
	const q = `INSERT INTO report_generation_run
		(project_id, tenant_id, stage, stage_progress, agent_log, output_type, artifact_key, char_count, duration_ms, status, error_message, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id`
	return r.pool.QueryRow(ctx, q,
		run.ProjectID, run.TenantID, run.Stage, string(run.StageProgress), string(run.AgentLog),
		run.OutputType, run.ArtifactKey, run.CharCount, run.DurationMS, run.Status, run.ErrorMessage, run.CreatedAt,
	).Scan(&run.ID)
}

func (r *postgresRepository) GetRun(ctx context.Context, id, tenantID int64) (*ReportGenerationRun, error) {
	const q = `SELECT id,project_id,tenant_id,stage,stage_progress,agent_log,output_type,artifact_key,char_count,duration_ms,status,error_message,created_at,finished_at
		FROM report_generation_run WHERE id=$1 AND tenant_id=$2`
	run := &ReportGenerationRun{}
	var progress, agentLog []byte
	err := r.pool.QueryRow(ctx, q, id, tenantID).Scan(
		&run.ID, &run.ProjectID, &run.TenantID, &run.Stage, &progress, &agentLog,
		&run.OutputType, &run.ArtifactKey, &run.CharCount, &run.DurationMS, &run.Status, &run.ErrorMessage,
		&run.CreatedAt, &run.FinishedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("%w: run %d", ErrNotFound, id)
	}
	if err != nil {
		return nil, err
	}
	run.StageProgress = json.RawMessage(progress)
	run.AgentLog = json.RawMessage(agentLog)
	return run, nil
}

func (r *postgresRepository) ListRuns(ctx context.Context, projectID, tenantID int64) ([]*ReportGenerationRun, error) {
	const q = `SELECT id,project_id,tenant_id,stage,stage_progress,agent_log,output_type,artifact_key,char_count,duration_ms,status,error_message,created_at,finished_at
		FROM report_generation_run WHERE project_id=$1 AND tenant_id=$2 ORDER BY id DESC`
	rows, err := r.pool.Query(ctx, q, projectID, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ReportGenerationRun
	for rows.Next() {
		run := &ReportGenerationRun{}
		var progress, agentLog []byte
		if err := rows.Scan(
			&run.ID, &run.ProjectID, &run.TenantID, &run.Stage, &progress, &agentLog,
			&run.OutputType, &run.ArtifactKey, &run.CharCount, &run.DurationMS, &run.Status, &run.ErrorMessage,
			&run.CreatedAt, &run.FinishedAt,
		); err != nil {
			return nil, err
		}
		run.StageProgress = json.RawMessage(progress)
		run.AgentLog = json.RawMessage(agentLog)
		out = append(out, run)
	}
	return out, rows.Err()
}

func (r *postgresRepository) UpdateRunStage(ctx context.Context, id int64, stage, status string, progress, agentLog json.RawMessage) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE report_generation_run SET stage=$2, status=$3, stage_progress=$4, agent_log=$5 WHERE id=$1`,
		id, stage, status, string(progress), string(agentLog))
	return err
}

func (r *postgresRepository) FinishRun(ctx context.Context, id int64, status, artifactKey string, charCount int, durationMS int64, errMsg string) error {
	_, err := r.pool.Exec(ctx,
		`UPDATE report_generation_run SET status=$2, artifact_key=$3, char_count=$4, duration_ms=$5, error_message=$6, finished_at=now(), stage='done' WHERE id=$1`,
		id, status, artifactKey, charCount, durationMS, errMsg)
	return err
}

// ---- 模板 ----

func (r *postgresRepository) CreateTemplate(ctx context.Context, t *ReportTemplate) error {
	t.CreatedAt = time.Now()
	if t.Version == 0 {
		t.Version = 1
	}
	if len(t.Layout) == 0 {
		b, _ := json.Marshal(DefaultLayout())
		t.Layout = b
	}
	const q = `INSERT INTO report_template (tenant_id, name, doc_type, version, layout, is_builtin, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id`
	return r.pool.QueryRow(ctx, q,
		t.TenantID, t.Name, t.DocType, t.Version, string(t.Layout), t.IsBuiltin, t.CreatedAt,
	).Scan(&t.ID)
}

func (r *postgresRepository) GetTemplate(ctx context.Context, id, tenantID int64) (*ReportTemplate, error) {
	const q = `SELECT id,tenant_id,name,doc_type,version,layout,is_builtin,created_at
		FROM report_template WHERE id=$1 AND tenant_id=$2`
	t := &ReportTemplate{}
	var layout []byte
	err := r.pool.QueryRow(ctx, q, id, tenantID).Scan(
		&t.ID, &t.TenantID, &t.Name, &t.DocType, &t.Version, &layout, &t.IsBuiltin, &t.CreatedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("%w: template %d", ErrNotFound, id)
	}
	if err != nil {
		return nil, err
	}
	t.Layout = json.RawMessage(layout)
	return t, nil
}

func (r *postgresRepository) ListTemplates(ctx context.Context, tenantID int64) ([]*ReportTemplate, error) {
	const q = `SELECT id,tenant_id,name,doc_type,version,layout,is_builtin,created_at
		FROM report_template WHERE tenant_id=$1 ORDER BY id DESC`
	rows, err := r.pool.Query(ctx, q, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*ReportTemplate
	for rows.Next() {
		t := &ReportTemplate{}
		var layout []byte
		if err := rows.Scan(&t.ID, &t.TenantID, &t.Name, &t.DocType, &t.Version, &layout, &t.IsBuiltin, &t.CreatedAt); err != nil {
			return nil, err
		}
		t.Layout = json.RawMessage(layout)
		out = append(out, t)
	}
	return out, rows.Err()
}
'''

# ============ 4. orchestrator.go（6 Agent 编排，确定性 mock）============
ORCHESTRATOR_GO = '''package docreport

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Orchestrator 6 Agent 编排（P2-02 设计 19 §5：PLAN/RETRIEVE/WRITE/DESIGN/REVIEW/RENDER）。
// 确定性 mock，不依赖 LLM；每个 Agent 输出可观测。
type Orchestrator struct{}

// NewOrchestrator 构造编排器。
func NewOrchestrator() *Orchestrator { return &Orchestrator{} }

// PlanAgent 根据需求生成大纲（P2-02 设计 19 §5：需求分析→大纲规划）。
func (o *Orchestrator) PlanAgent(topic, audience, docType string) ([]OutlineSection, AgentStep) {
	start := time.Now()
	// 确定性大纲：根据主题生成 5 章
	sections := []OutlineSection{
		{Title: fmt.Sprintf("《%s》概述", topic), OrderIdx: 0},
		{Title: "背景与目标", OrderIdx: 1},
		{Title: "核心内容分析", OrderIdx: 2},
		{Title: "实施方案与路径", OrderIdx: 3},
		{Title: "总结与展望", OrderIdx: 4},
	}
	step := AgentStep{
		Agent:      "plan",
		Stage:      "plan",
		Status:     "done",
		Message:    fmt.Sprintf("生成 %d 章大纲，受众=%s，文档类型=%s", len(sections), audience, docType),
		Tokens:     320,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return sections, step
}

// RetrieveAgent 从知识库检索相关资料（mock，P2-02 设计 19 §5：资料检索）。
func (o *Orchestrator) RetrieveAgent(topic string, sections []OutlineSection) (map[string]string, AgentStep) {
	start := time.Now()
	// mock：为每章生成确定性参考文本
	references := make(map[string]string, len(sections))
	for _, s := range sections {
		references[s.Title] = fmt.Sprintf("参考资料显示，%s 与主题「%s」密切相关。本节将围绕该方向展开详细分析，提供数据支撑与案例佐证。", s.Title, topic)
	}
	step := AgentStep{
		Agent:      "retrieve",
		Stage:      "retrieve",
		Status:     "done",
		Message:    fmt.Sprintf("检索 %d 章参考资料", len(sections)),
		Tokens:     180,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return references, step
}

// WriteAgent 根据大纲+资料生成各章节正文（mock，P2-02 设计 19 §5：内容写作）。
func (o *Orchestrator) WriteAgent(topic, audience string, sections []OutlineSection, refs map[string]string) ([]ReportSection, AgentStep) {
	start := time.Now()
	written := make([]ReportSection, 0, len(sections))
	for i, s := range sections {
		content := fmt.Sprintf("## %s\\n\\n%s\\n\\n本节针对「%s」的受众（%s）展开。核心要点：\\n\\n1. **定位**：%s 的战略价值与业务意义\\n2. **方法**：采用数据驱动 + 多维度交叉验证\\n3. **结论**：%s 的实施可量化、可追踪、可复用\\n\\n> %s\\n\\n（mock 生成，字符数 %d）",
			s.Title,
			refs[s.Title],
			topic,
			audience,
			topic,
			topic,
			refs[s.Title],
			200+i*50,
		)
		written = append(written, ReportSection{
			Title:    s.Title,
			Content:  content,
			OrderIdx: i,
			Status:   "written",
		})
	}
	step := AgentStep{
		Agent:      "write",
		Stage:      "write",
		Status:     "done",
		Message:    fmt.Sprintf("生成 %d 章正文，总字符 %d", len(written), o.totalChars(written)),
		Tokens:     1280,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return written, step
}

// DesignAgent 根据内容设计版式（mock，P2-02 设计 19 §5：PPT 设计/Word 排版）。
func (o *Orchestrator) DesignAgent(docType string, sections []ReportSection) (map[string]any, AgentStep) {
	start := time.Now()
	// mock：返回版式设计（章节-页码映射）
	pages := make([]map[string]any, 0, len(sections))
	for i, s := range sections {
		pages = append(pages, map[string]any{
			"section": s.Title,
			"page":    i + 1,
			"layout":  "title+content",
			"chart":   i%2 == 0, // 偶数章插入图表
		})
	}
	design := map[string]any{
		"docType":   docType,
		"pages":     pages,
		"theme":     "corporate",
		"primaryColor": "#1a73e8",
	}
	step := AgentStep{
		Agent:      "design",
		Stage:      "design",
		Status:     "done",
		Message:    fmt.Sprintf("设计 %d 页版式，主题=corporate", len(pages)),
		Tokens:     240,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return design, step
}

// ReviewAgent 审查内容质量（mock，P2-02 设计 19 §5：合规审查）。
func (o *Orchestrator) ReviewAgent(sections []ReportSection) (ReviewResult, AgentStep) {
	start := time.Now()
	issues := []string{}
	totalChars := o.totalChars(sections)
	if totalChars < 100 {
		issues = append(issues, "内容过短，建议补充")
	}
	for _, s := range sections {
		if strings.TrimSpace(s.Content) == "" {
			issues = append(issues, fmt.Sprintf("章节「%s」内容为空", s.Title))
		}
	}
	score := 0.92
	if len(issues) > 0 {
		score = 0.75
	}
	result := ReviewResult{
		Score:     score,
		Issues:    issues,
		Approved:  len(issues) == 0,
		Suggestion: "整体内容完整，建议加强数据支撑",
	}
	step := AgentStep{
		Agent:      "review",
		Stage:      "review",
		Status:     "done",
		Message:    fmt.Sprintf("审查评分 %.2f，问题 %d 项", score, len(issues)),
		Tokens:     160,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return result, step
}

// RenderAgent 渲染为目标格式（P2-02 设计 19 §6：PPT/Word/PDF/Markdown）。
func (o *Orchestrator) RenderAgent(outputType string, project *ReportProject, sections []ReportSection, design map[string]any) (ReportArtifact, AgentStep) {
	start := time.Now()
	var content string
	var mimeType, fileName string
	switch outputType {
	case "markdown":
		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("# %s\\n\\n**主题**：%s  **受众**：%s\\n\\n---\\n\\n", project.Title, project.Topic, project.Audience))
		for _, s := range sections {
			sb.WriteString(s.Content)
			sb.WriteString("\\n\\n---\\n\\n")
		}
		content = sb.String()
		mimeType = "text/markdown"
		fileName = project.Title + ".md"
	case "ppt":
		// mock PPT 结构：每章一页
		pages := make([]map[string]any, 0, len(sections))
		for i, s := range sections {
			pages = append(pages, map[string]any{
				"page":    i + 1,
				"title":   s.Title,
				"content": s.Content,
				"layout":  "title+content",
			})
		}
		b, _ := json.Marshal(map[string]any{
			"title":     project.Title,
			"topic":     project.Topic,
			"audience":  project.Audience,
			"pages":     pages,
			"design":    design,
			"version":   "mock-ppt-v1",
		})
		content = string(b)
		mimeType = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
		fileName = project.Title + ".pptx"
	case "word":
		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("# %s\\n\\n", project.Title))
		sb.WriteString(fmt.Sprintf("主题：%s\\n受众：%s\\n\\n", project.Topic, project.Audience))
		for _, s := range sections {
			sb.WriteString(s.Content)
			sb.WriteString("\\n\\n")
		}
		content = sb.String()
		mimeType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
		fileName = project.Title + ".docx"
	case "pdf":
		// mock PDF 结构
		b, _ := json.Marshal(map[string]any{
			"title":    project.Title,
			"topic":    project.Topic,
			"audience": project.Audience,
			"sections": sections,
			"design":   design,
			"version":   "mock-pdf-v1",
		})
		content = string(b)
		mimeType = "application/pdf"
		fileName = project.Title + ".pdf"
	default:
		content = fmt.Sprintf("# %s\\n(unsupported type: %s)", project.Title, outputType)
		mimeType = "text/plain"
		fileName = project.Title + ".txt"
	}
	artifact := ReportArtifact{
		DocType:    outputType,
		FileName:   fileName,
		Content:    content,
		MimeType:   mimeType,
		Size:       len(content),
		Sections:   sections,
		CharCount:  len(content),
		ArtifactKey: fmt.Sprintf("docreport/%d/%s", project.ID, fileName),
	}
	step := AgentStep{
		Agent:      "render",
		Stage:      "render",
		Status:     "done",
		Message:    fmt.Sprintf("渲染 %s，%d 字节，文件=%s", outputType, len(content), fileName),
		Tokens:     80,
		DurationMS: time.Since(start).Milliseconds(),
	}
	return artifact, step
}

// totalChars 计算章节总字符数。
func (o *Orchestrator) totalChars(sections []ReportSection) int {
	total := 0
	for _, s := range sections {
		total += len(s.Content)
	}
	return total
}

// ReviewResult 审查结果。
type ReviewResult struct {
	Score      float64 `json:"score"`
	Issues     []string `json:"issues"`
	Approved   bool    `json:"approved"`
	Suggestion string  `json:"suggestion"`
}
'''

# ============ 5. service.go ============
SERVICE_GO = '''package docreport

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// Service 文档报告生成（P2-02）业务逻辑。
type Service struct {
	repo  Repository
	orch  *Orchestrator
}

// NewService 用仓储构造 Service。
func NewService(repo Repository) *Service {
	return &Service{repo: repo, orch: NewOrchestrator()}
}

// CreateProject 创建报告项目（含默认大纲回填）。
func (s *Service) CreateProject(in CreateProjectInput) (*ReportProject, error) {
	if in.Title == "" {
		return nil, fmt.Errorf("%w: title required", ErrInvalidInput)
	}
	dt := in.DocType
	if !isValidDocType(dt) {
		dt = "markdown"
	}
	// 调用 Plan Agent 生成初始大纲
	sections, planStep := s.orch.PlanAgent(in.Topic, in.Audience, dt)
	outlineBytes, _ := json.Marshal(sections)
	p := &ReportProject{
		TenantID:   in.TenantID,
		Title:      in.Title,
		DocType:    dt,
		Topic:      in.Topic,
		Audience:   in.Audience,
		TemplateID: in.TemplateID,
		CreatedBy:  in.CreatedBy,
		Status:     "draft",
		Outline:    outlineBytes,
	}
	if err := s.repo.CreateProject(context.Background(), p); err != nil {
		return nil, err
	}
	// 落库大纲章节
	ss := make([]*ReportSection, 0, len(sections))
	for _, o := range sections {
		ss = append(ss, &ReportSection{Title: o.Title, OrderIdx: o.OrderIdx, Status: "pending"})
	}
	_ = s.repo.UpsertSections(context.Background(), p.ID, p.TenantID, ss)
	_ = planStep // 不记录到 run，CreateProject 不创建 run
	return p, nil
}

// GetProject 获取项目详情。
func (s *Service) GetProject(id, tenantID int64) (*ReportProject, error) {
	return s.repo.GetProject(context.Background(), id, tenantID)
}

// ListProjects 列出项目。
func (s *Service) ListProjects(tenantID int64) ([]*ReportProject, error) {
	return s.repo.ListProjects(context.Background(), tenantID)
}

// UpdateOutline 更新大纲（P2-02 设计 19 §4：需求澄清→大纲）。
func (s *Service) UpdateOutline(projectID, tenantID int64, in UpdateOutlineInput) ([]*ReportSection, error) {
	if _, err := s.repo.GetProject(context.Background(), projectID, tenantID); err != nil {
		return nil, err
	}
	if len(in.Sections) == 0 {
		return nil, fmt.Errorf("%w: at least one section required", ErrInvalidInput)
	}
	ss := make([]*ReportSection, 0, len(in.Sections))
	for _, o := range in.Sections {
		ss = append(ss, &ReportSection{Title: o.Title, OrderIdx: o.OrderIdx, Status: "pending"})
	}
	if err := s.repo.UpsertSections(context.Background(), projectID, tenantID, ss); err != nil {
		return nil, err
	}
	// 同步 outline 到 project
	outlineBytes, _ := json.Marshal(in.Sections)
	_ = s.repo.UpdateProjectStatus(context.Background(), projectID, "draft", "")
	_ = outlineBytes
	return s.repo.ListSections(context.Background(), projectID, tenantID)
}

// ListSections 列出章节。
func (s *Service) ListSections(projectID, tenantID int64) ([]*ReportSection, error) {
	return s.repo.ListSections(context.Background(), projectID, tenantID)
}

// Generate 生成报告（6 Agent 编排，P2-02 设计 19 §4：9 步流水线）。
func (s *Service) Generate(projectID, tenantID int64, in GenerateInput) (*ReportGenerationRun, *ReportArtifact, error) {
	project, err := s.repo.GetProject(context.Background(), projectID, tenantID)
	if err != nil {
		return nil, nil, err
	}
	outputType := in.OutputType
	if outputType == "" {
		outputType = project.DocType
	}
	if !isValidDocType(outputType) {
		return nil, nil, fmt.Errorf("%w: invalid outputType %s", ErrInvalidInput, outputType)
	}
	startTime := time.Now()
	// 创建 run
	run := &ReportGenerationRun{
		ProjectID:  projectID,
		TenantID:   tenantID,
		Stage:      "plan",
		OutputType: outputType,
		Status:     "running",
	}
	if err := s.repo.CreateRun(context.Background(), run); err != nil {
		return nil, nil, err
	}
	// 更新项目状态
	_ = s.repo.UpdateProjectStatus(context.Background(), projectID, "generating", "")

	agentLog := []AgentStep{}
	progress := map[string]string{}

	// 1. PLAN
	sections, planStep := s.orch.PlanAgent(project.Topic, project.Audience, outputType)
	agentLog = append(agentLog, planStep)
	progress["plan"] = "done"
	pb, _ := json.Marshal(progress)
	ab, _ := json.Marshal(agentLog)
	_ = s.repo.UpdateRunStage(context.Background(), run.ID, "retrieve", "running", pb, ab)

	// 2. RETRIEVE
	refs, retrieveStep := s.orch.RetrieveAgent(project.Topic, sections)
	agentLog = append(agentLog, retrieveStep)
	progress["retrieve"] = "done"
	pb, _ = json.Marshal(progress)
	ab, _ = json.Marshal(agentLog)
	_ = s.repo.UpdateRunStage(context.Background(), run.ID, "write", "running", pb, ab)

	// 3. WRITE
	written, writeStep := s.orch.WriteAgent(project.Topic, project.Audience, sections, refs)
	agentLog = append(agentLog, writeStep)
	progress["write"] = "done"
	pb, _ = json.Marshal(progress)
	ab, _ = json.Marshal(agentLog)
	_ = s.repo.UpdateRunStage(context.Background(), run.ID, "design", "running", pb, ab)
	// 落库章节正文
	ss := make([]*ReportSection, 0, len(written))
	for _, w := range written {
		wc := w
		ss = append(ss, &wc)
	}
	_ = s.repo.UpsertSections(context.Background(), projectID, tenantID, ss)

	// 4. DESIGN
	design, designStep := s.orch.DesignAgent(outputType, written)
	agentLog = append(agentLog, designStep)
	progress["design"] = "done"
	pb, _ = json.Marshal(progress)
	ab, _ = json.Marshal(agentLog)
	_ = s.repo.UpdateRunStage(context.Background(), run.ID, "review", "running", pb, ab)

	// 5. REVIEW
	review, reviewStep := s.orch.ReviewAgent(written)
	agentLog = append(agentLog, reviewStep)
	progress["review"] = "done"
	pb, _ = json.Marshal(progress)
	ab, _ = json.Marshal(agentLog)
	_ = s.repo.UpdateRunStage(context.Background(), run.ID, "render", "running", pb, ab)

	// 6. RENDER
	artifact, renderStep := s.orch.RenderAgent(outputType, project, written, design)
	agentLog = append(agentLog, renderStep)
	progress["render"] = "done"
	pb, _ = json.Marshal(progress)
	ab, _ = json.Marshal(agentLog)

	durationMS := time.Since(startTime).Milliseconds()
	status := "done"
	if !review.Approved {
		status = "done"
		// mock：即使有 issues 也完成生成（保留审查记录）
	}
	_ = s.repo.FinishRun(context.Background(), run.ID, status, artifact.ArtifactKey, artifact.CharCount, durationMS, "")
	_ = s.repo.UpdateProjectStatus(context.Background(), projectID, "done", artifact.ArtifactKey)

	// 重新读取 run 以拿到最新状态
	updatedRun, _ := s.repo.GetRun(context.Background(), run.ID, tenantID)
	if updatedRun != nil {
		run = updatedRun
	}
	return run, &artifact, nil
}

// GetRun 查询生成运行。
func (s *Service) GetRun(id, tenantID int64) (*ReportGenerationRun, error) {
	return s.repo.GetRun(context.Background(), id, tenantID)
}

// ListRuns 列出项目的生成运行。
func (s *Service) ListRuns(projectID, tenantID int64) ([]*ReportGenerationRun, error) {
	return s.repo.ListRuns(context.Background(), projectID, tenantID)
}

// Export 导出报告（P2-02 设计 19 §11：POST /reports/{id}/export）。
func (s *Service) Export(projectID, tenantID int64, in ExportInput) (*ReportArtifact, error) {
	project, err := s.repo.GetProject(context.Background(), projectID, tenantID)
	if err != nil {
		return nil, err
	}
	outputType := in.OutputType
	if outputType == "" {
		outputType = project.DocType
	}
	if !isValidDocType(outputType) {
		return nil, fmt.Errorf("%w: invalid outputType %s", ErrInvalidInput, outputType)
	}
	sections, err := s.repo.ListSections(context.Background(), projectID, tenantID)
	if err != nil {
		return nil, err
	}
	// 转换为 ReportSection 值类型
	written := make([]ReportSection, 0, len(sections))
	for _, s := range sections {
		written = append(written, *s)
	}
	design, _ := s.orch.DesignAgent(outputType, written)
	artifact, _ := s.orch.RenderAgent(outputType, project, written, design)
	return &artifact, nil
}

// ---- 模板 ----

// CreateTemplate 创建模板（P2-02 设计 19 §9：模板版本化）。
func (s *Service) CreateTemplate(in CreateTemplateInput) (*ReportTemplate, error) {
	if in.Name == "" {
		return nil, fmt.Errorf("%w: name required", ErrInvalidInput)
	}
	dt := in.DocType
	if !isValidDocType(dt) {
		dt = "markdown"
	}
	t := &ReportTemplate{
		TenantID: in.TenantID,
		Name:     in.Name,
		DocType:  dt,
		Layout:   in.Layout,
	}
	if err := s.repo.CreateTemplate(context.Background(), t); err != nil {
		return nil, err
	}
	return t, nil
}

// ListTemplates 列出模板。
func (s *Service) ListTemplates(tenantID int64) ([]*ReportTemplate, error) {
	return s.repo.ListTemplates(context.Background(), tenantID)
}

// isValidDocType 校验文档类型。
func isValidDocType(dt string) bool {
	if dt == "" {
		return false
	}
	for _, t := range SupportedDocTypes {
		if t == dt {
			return true
		}
	}
	return false
}
'''

# ============ 6. handler.go ============
HANDLER_GO = '''package docreport

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strconv"
)

// Handler 暴露文档报告生成（P2-02）的 HTTP 接口。
type Handler struct {
	svc *Service
}

// NewHandler 用 Service 构造 Handler。
func NewHandler(svc *Service) *Handler { return &Handler{svc: svc} }

// Register 在 /api/v1/doc-report 下挂载全部路由（与 gbi/opt 同构）。
func (h *Handler) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/doc-report/projects", h.createProject)
	mux.HandleFunc("GET /api/v1/doc-report/projects", h.listProjects)
	mux.HandleFunc("GET /api/v1/doc-report/projects/{id}", h.getProject)
	mux.HandleFunc("PUT /api/v1/doc-report/projects/{id}/outline", h.updateOutline)
	mux.HandleFunc("GET /api/v1/doc-report/projects/{id}/sections", h.listSections)
	mux.HandleFunc("POST /api/v1/doc-report/projects/{id}/generate", h.generate)
	mux.HandleFunc("GET /api/v1/doc-report/projects/{id}/runs", h.listRuns)
	mux.HandleFunc("GET /api/v1/doc-report/runs/{runId}", h.getRun)
	mux.HandleFunc("POST /api/v1/doc-report/projects/{id}/export", h.export)
	mux.HandleFunc("POST /api/v1/doc-report/templates", h.createTemplate)
	mux.HandleFunc("GET /api/v1/doc-report/templates", h.listTemplates)
}

func (h *Handler) createProject(w http.ResponseWriter, r *http.Request) {
	var in CreateProjectInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeError(w, fmt.Errorf("%w: bad json", ErrInvalidInput))
		return
	}
	if in.TenantID == 0 {
		in.TenantID = queryInt64(r, "tenantId")
	}
	v, err := h.svc.CreateProject(in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, v)
}

func (h *Handler) listProjects(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	list, err := h.svc.ListProjects(tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, list)
}

func (h *Handler) getProject(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	v, err := h.svc.GetProject(id, tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, v)
}

func (h *Handler) updateOutline(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	var in UpdateOutlineInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeError(w, fmt.Errorf("%w: bad json", ErrInvalidInput))
		return
	}
	if in.TenantID == 0 {
		in.TenantID = tenantID
	}
	list, err := h.svc.UpdateOutline(id, tenantID, in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, list)
}

func (h *Handler) listSections(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	list, err := h.svc.ListSections(id, tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, list)
}

func (h *Handler) generate(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	var in GenerateInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		// 允许空 body，用默认值
		in = GenerateInput{TenantID: tenantID}
	}
	if in.TenantID == 0 {
		in.TenantID = tenantID
	}
	run, artifact, err := h.svc.Generate(id, tenantID, in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, map[string]any{"run": run, "artifact": artifact})
}

func (h *Handler) listRuns(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	list, err := h.svc.ListRuns(id, tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, list)
}

func (h *Handler) getRun(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	runID, err := pathInt64(r, "runId")
	if err != nil {
		writeError(w, err)
		return
	}
	v, err := h.svc.GetRun(runID, tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, v)
}

func (h *Handler) export(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	id, err := pathInt64(r, "id")
	if err != nil {
		writeError(w, err)
		return
	}
	var in ExportInput
	_ = json.NewDecoder(r.Body).Decode(&in)
	if in.TenantID == 0 {
		in.TenantID = tenantID
	}
	artifact, err := h.svc.Export(id, tenantID, in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, artifact)
}

func (h *Handler) createTemplate(w http.ResponseWriter, r *http.Request) {
	var in CreateTemplateInput
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		writeError(w, fmt.Errorf("%w: bad json", ErrInvalidInput))
		return
	}
	if in.TenantID == 0 {
		in.TenantID = queryInt64(r, "tenantId")
	}
	v, err := h.svc.CreateTemplate(in)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, v)
}

func (h *Handler) listTemplates(w http.ResponseWriter, r *http.Request) {
	tenantID := queryInt64(r, "tenantId")
	list, err := h.svc.ListTemplates(tenantID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeResponse(w, http.StatusOK, list)
}

// ---- 共享辅助（与 gbi/opt 同构）----

func queryInt64(r *http.Request, key string) int64 {
	v := r.URL.Query().Get(key)
	if v == "" {
		return 0
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

func pathInt64(r *http.Request, key string) (int64, error) {
	v := r.PathValue(key)
	if v == "" {
		return 0, fmt.Errorf("%w: missing path param %s", ErrInvalidInput, key)
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("%w: invalid path param %s", ErrInvalidInput, key)
	}
	return n, nil
}

func writeResponse(w http.ResponseWriter, code int, data any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "msg": "ok", "data": normalizeNilSlice(data)})
}

func normalizeNilSlice(v any) any {
	if v == nil {
		return v
	}
	rv := reflect.ValueOf(v)
	if rv.Kind() == reflect.Slice && rv.IsNil() {
		return []any{}
	}
	return v
}

func writeError(w http.ResponseWriter, err error) {
	code := http.StatusInternalServerError
	if errors.Is(err, ErrInvalidInput) {
		code = http.StatusBadRequest
	} else if errors.Is(err, ErrNotFound) {
		code = http.StatusNotFound
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]any{"code": code, "msg": err.Error(), "data": nil})
}
'''

# 写入所有文件
files = {
	'model.go': MODEL_GO,
	'repository.go': REPOSITORY_GO,
	'postgres_repository.go': POSTGRES_REPOSITORY_GO,
	'orchestrator.go': ORCHESTRATOR_GO,
	'service.go': SERVICE_GO,
	'handler.go': HANDLER_GO,
}

for name, content in files.items():
	path = os.path.join(PKG_DIR, name)
	with open(path, 'w', encoding='utf-8') as f:
		f.write(content)
	print(f'OK: {name} ({len(content)} bytes)')

print('ALL DONE: docreport package created')
