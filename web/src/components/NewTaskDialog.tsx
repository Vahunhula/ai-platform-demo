import { useEffect, useRef, useState, type DragEvent, type FormEvent } from "react";

import { api } from "../api/client";
import type { AssignableUser, CreateTaskResponse, RepositoryOption } from "../types/api";

interface Props {
  onClose: () => void;
  onCreated: (task: CreateTaskResponse) => void;
}

export function NewTaskDialog({ onClose, onCreated }: Props) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [repositories, setRepositories] = useState<RepositoryOption[]>([]);
  const [users, setUsers] = useState<AssignableUser[]>([]);
  const [repositoryId, setRepositoryId] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [assigneeId, setAssigneeId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [jiraKey, setJiraKey] = useState("");
  const [testStories, setTestStories] = useState("");
  const [testFiles, setTestFiles] = useState<{ filename: string; content: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api.listRepositories(controller.signal),
      api.listAssignableUsers(controller.signal),
    ])
      .then(([nextRepositories, nextUsers]) => {
        setRepositories(nextRepositories);
        setUsers(nextUsers);
        const firstRepository = nextRepositories[0];
        if (firstRepository) {
          setRepositoryId(firstRepository.id);
          setBaseBranch(firstRepository.default_branch);
        }
        setAssigneeId(nextUsers[0]?.id ?? "");
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const chooseRepository = (id: string) => {
    setRepositoryId(id);
    setBaseBranch(repositories.find((repository) => repository.id === id)?.default_branch ?? "");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const created = await api.createTask({
        title,
        description,
        repository_id: repositoryId,
        base_branch: baseBranch,
        assignee_user_id: assigneeId,
        jira_key: jiraKey.trim() || null,
        acceptance_test_stories: testStories.trim() ? testStories : null,
        uploaded_test_files: testFiles,
      });
      onCreated(created);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setSubmitting(false);
    }
  };

  const selectFiles = async (files: File[]) => {
    const allowed = new Set([".py", ".js", ".ts", ".json", ".yaml", ".yml"]);
    const unsupported = files.find((file) => {
      const extension = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
      return !allowed.has(extension);
    });
    if (unsupported) {
      setTestFiles([]);
      setError(`Unsupported test file: ${unsupported.name}. Use .py, .js, .ts, .json, .yaml, or .yml.`);
      return;
    }
    try {
      setTestFiles(await Promise.all(files.map(async (file) => ({ filename: file.name, content: await file.text() }))));
      setError(null);
    } catch {
      setError("The selected test files could not be read.");
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="new-task-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-task-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
            <span className="eyebrow">AI Platform</span>
            <h2 id="new-task-title">New Task</h2>
          </div>
          <button className="dialog-close" onClick={onClose} aria-label="Close">×</button>
        </div>
        <form onSubmit={submit}>
          <section className="form-section">
            <h3>Basic information</h3>
            <label>Title <span aria-hidden="true">*</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} required autoFocus />
            </label>
            <label>Description <span aria-hidden="true">*</span>
              <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={10000} rows={4} required />
            </label>
            <div className="form-grid">
              <label>Repository <span aria-hidden="true">*</span>
                <select value={repositoryId} onChange={(event) => chooseRepository(event.target.value)} required>
                  {repositories.map((repository) => <option key={repository.id} value={repository.id}>{repository.display_name}</option>)}
                </select>
              </label>
              <label>Base branch<input value={baseBranch} readOnly aria-readonly="true" /></label>
              <label>Assignee
                <select value={assigneeId} onChange={(event) => setAssigneeId(event.target.value)} required>
                  {users.map((user) => <option key={user.id} value={user.id}>{user.display_name}</option>)}
                </select>
              </label>
              <label>Jira key <small>optional</small>
                <input value={jiraKey} onChange={(event) => setJiraKey(event.target.value)} maxLength={80} placeholder="e.g. ENG-123" />
              </label>
              <label>Starting phase<select value="BRAINSTORM" disabled><option>Brainstorm</option></select></label>
            </div>
          </section>
          <fieldset className="optional-tests">
            <legend>Tests <small>optional</small></legend>
            <p>Human-written requirements are canonical. Claude can convert them into executable task tests.</p>
            <label>
              Acceptance / user stories
              <textarea
                value={testStories}
                onChange={(event) => setTestStories(event.target.value)}
                maxLength={20000}
                rows={5}
                placeholder={"Example:\n- Quantity 10 should be accepted\n- Quantity 15 should be rejected"}
              />
            </label>
            <div className="upload-label">Upload test files</div>
            <div
              className="file-drop"
              role="button"
              tabIndex={0}
              onClick={() => fileInput.current?.click()}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") fileInput.current?.click(); }}
              onDragOver={(event: DragEvent) => event.preventDefault()}
              onDrop={(event: DragEvent) => { event.preventDefault(); void selectFiles([...event.dataTransfer.files]); }}
            >
              <strong>Drop test files here or Browse</strong>
              <span>.py .js .ts .json .yaml .yml · up to 10 files</span>
              <input
                ref={fileInput}
                className="visually-hidden"
                type="file"
                multiple
                accept=".py,.js,.ts,.json,.yaml,.yml"
                aria-label="Upload test files"
                onChange={(event) => void selectFiles([...(event.target.files ?? [])])}
              />
            </div>
            {testFiles.length > 0 && <ul className="selected-files">{testFiles.map((file) => <li key={file.filename}><span>{file.filename}</span><button type="button" aria-label={`Remove ${file.filename}`} onClick={() => setTestFiles((current) => current.filter((item) => item.filename !== file.filename))}>×</button></li>)}</ul>}
          </fieldset>
          <p className="phase-note">Creates a READY task in Brainstorm with an isolated workspace.</p>
          {error && <div className="form-error">{error}</div>}
          {!loading && repositories.length === 0 && <div className="form-error">No repositories are registered.</div>}
          <div className="dialog-actions">
            <button type="button" className="secondary-button" onClick={onClose} disabled={submitting}>Cancel</button>
            <button type="submit" className="primary-button" disabled={loading || submitting || !repositoryId || !assigneeId}>
              {submitting ? "Creating workspace…" : "Create Task"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
