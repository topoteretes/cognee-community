"""Test fixtures for the Ansible connector test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_ansible_project(tmp_path: Path) -> Path:
    """Create a complete fixture Ansible project containing all required components:

    - One inventory with multiple hosts and groups (INI & YAML)
    - Parent-child inventory groups
    - Two playbooks
    - Three roles with a dependency chain (app -> web -> common)
    - Tasks and handlers
    - Default variables
    - A vaulted variable file ($ANSIBLE_VAULT)
    - Group variables and host variables (with secret values that must not leak)
    """
    root = tmp_path / "test_ansible_repo"
    root.mkdir()

    # 1. INI Inventory
    ini_content = """
# Ungrouped host
bastion.example.com

[webservers]
web1.example.com http_port=80 secret_token=LEAKY_HOST_TOKEN_123
web2.example.com http_port=80

[dbservers]
db1.example.com db_password=LEAKY_DB_PASSWORD_456

[appservers]
app1.example.com
app2.example.com

[production:children]
webservers
appservers
dbservers

[webservers:vars]
group_secret=LEAKY_GROUP_VAR_789
"""
    (root / "inventory.ini").write_text(ini_content.strip(), encoding="utf-8")

    # 2. Roles
    # Role 1: common
    common_tasks = root / "roles" / "common" / "tasks"
    common_tasks.mkdir(parents=True)
    (common_tasks / "main.yml").write_text(
        """
- name: Install base packages
  apt:
    name: [curl, git]
    state: present

- name: Configure SSH daemon
  service:
    name: sshd
    state: started
""".strip(),
        encoding="utf-8",
    )

    common_handlers = root / "roles" / "common" / "handlers"
    common_handlers.mkdir(parents=True)
    (common_handlers / "main.yml").write_text(
        """
- name: Restart sshd
  service:
    name: sshd
    state: restarted
""".strip(),
        encoding="utf-8",
    )

    common_defaults = root / "roles" / "common" / "defaults"
    common_defaults.mkdir(parents=True)
    (common_defaults / "main.yml").write_text(
        """
common_base_packages: [curl, git]
ssh_port: 22
sensitive_common_default: SECRET_COMMON_VAL_999
""".strip(),
        encoding="utf-8",
    )

    common_meta = root / "roles" / "common" / "meta"
    common_meta.mkdir(parents=True)
    (common_meta / "main.yml").write_text("dependencies: []\n", encoding="utf-8")

    # Role 2: web (depends on common)
    web_tasks = root / "roles" / "web" / "tasks"
    web_tasks.mkdir(parents=True)
    (web_tasks / "main.yml").write_text(
        """
- name: Install nginx package
  apt:
    name: nginx
    state: present

- name: Configure nginx virtual host
  template:
    src: vhost.j2
    dest: /etc/nginx/sites-available/default
  notify: Reload nginx

- name: Ensure nginx service is running
  service:
    name: nginx
    state: started
""".strip(),
        encoding="utf-8",
    )

    web_handlers = root / "roles" / "web" / "handlers"
    web_handlers.mkdir(parents=True)
    (web_handlers / "main.yml").write_text(
        """
- name: Reload nginx
  service:
    name: nginx
    state: reloaded

- name: Restart nginx
  service:
    name: nginx
    state: restarted
""".strip(),
        encoding="utf-8",
    )

    web_defaults = root / "roles" / "web" / "defaults"
    web_defaults.mkdir(parents=True)
    (web_defaults / "main.yml").write_text(
        """
http_port: 80
server_name: example.com
secret_web_key: SECRET_WEB_VAL_888
""".strip(),
        encoding="utf-8",
    )

    web_meta = root / "roles" / "web" / "meta"
    web_meta.mkdir(parents=True)
    (web_meta / "main.yml").write_text(
        """
dependencies:
  - role: common
""".strip(),
        encoding="utf-8",
    )

    # Role 3: app (depends on web -> dependency chain: app -> web -> common)
    app_tasks = root / "roles" / "app" / "tasks"
    app_tasks.mkdir(parents=True)
    (app_tasks / "main.yml").write_text(
        """
- name: Deploy application code
  git:
    repo: https://github.com/example/app.git
    dest: /opt/app

- name: Run database migrations
  command: /opt/app/bin/migrate
""".strip(),
        encoding="utf-8",
    )

    app_handlers = root / "roles" / "app" / "handlers"
    app_handlers.mkdir(parents=True)
    (app_handlers / "main.yml").write_text(
        """
- name: Restart application
  service:
    name: app
    state: restarted
""".strip(),
        encoding="utf-8",
    )

    app_defaults = root / "roles" / "app" / "defaults"
    app_defaults.mkdir(parents=True)
    (app_defaults / "main.yml").write_text(
        """
app_env: production
app_port: 8080
secret_app_token: SECRET_APP_VAL_777
""".strip(),
        encoding="utf-8",
    )

    app_meta = root / "roles" / "app" / "meta"
    app_meta.mkdir(parents=True)
    (app_meta / "main.yml").write_text(
        """
dependencies:
  - role: web
""".strip(),
        encoding="utf-8",
    )

    # 3. Two Playbooks
    # Playbook 1: site.yml
    (root / "site.yml").write_text(
        """
- name: Apply common setup to all servers
  hosts: all
  roles:
    - common
  tasks:
    - name: Verify basic connectivity
      ping:

- name: Deploy web servers
  hosts: webservers
  roles:
    - web
  tasks:
    - name: Check web health status
      uri:
        url: http://localhost/health
""".strip(),
        encoding="utf-8",
    )

    # Playbook 2: deploy_app.yml in playbooks/ subdirectory
    pb_dir = root / "playbooks"
    pb_dir.mkdir(parents=True)
    (pb_dir / "deploy_app.yml").write_text(
        """
- name: Deploy application stack
  hosts: appservers
  roles:
    - app
  tasks:
    - name: Warm up application cache
      uri:
        url: http://localhost:8080/warmup
""".strip(),
        encoding="utf-8",
    )

    # 4. Vaulted Variable File
    group_vars_dir = root / "group_vars" / "all"
    group_vars_dir.mkdir(parents=True)
    (group_vars_dir / "vault.yml").write_text(
        """$ANSIBLE_VAULT;1.1;AES256
36653335383561346332663936643936626435323237373836366539303362393231336437653531
3562303031313437643538613433613661643465386466370a356434383162313664333633643337
""".strip(),
        encoding="utf-8",
    )

    # 5. Group Variables & Host Variables
    (root / "group_vars" / "all.yml").write_text(
        """
global_domain: example.com
api_secret_key: ULTRA_SECRET_API_KEY_12345
db_master_password: SUPER_SECRET_MASTER_PASSWORD_67890
""".strip(),
        encoding="utf-8",
    )

    host_vars_dir = root / "host_vars"
    host_vars_dir.mkdir(parents=True)
    (host_vars_dir / "web1.example.com.yml").write_text(
        """
custom_host_cert: /etc/ssl/cert.pem
ssh_private_key: PRIVATE_KEY_SECRET_PLAINTEXT_DO_NOT_LEAK
""".strip(),
        encoding="utf-8",
    )

    return root
