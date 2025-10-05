import requests
import os
import time
import sys
import threading
import subprocess
import webbrowser
import json
import customtkinter as ctk
import gc

# --- IMPORTS POUR LA GESTION EN ARRIÈRE-PLAN ---
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pystray
from PIL import Image
from io import StringIO

# ======================================================================
# --- REDIRECTION DE LA CONSOLE VERS L'INTERFACE GRAPHIQUE ---
# ======================================================================

class ConsoleRedirector(object):
    """Redirige les sorties (print) vers un widget Text ou un label de CTk."""
    def __init__(self, output_widget, original_stdout):
        self.output_widget = output_widget
        self.original_stdout = original_stdout

    def write(self, s):
        def _insert_text():
            if self.output_widget.winfo_exists():
                self.output_widget.insert(ctk.END, s)
                self.output_widget.see(ctk.END)

        self.output_widget.after(0, _insert_text)

        self.original_stdout.write(s)
        self.original_stdout.flush()

    def flush(self):
        pass

# ======================================================================
# --- CONFIGURATION GLOBALE et UTILITAIRES PERSISTANTS ---
# ======================================================================
GITHUB_API_URL = "https://api.github.com"
TOKEN_FILE = "sync_token.txt"
CONFIG_FILE = "sync_config.json"
# Liste de base des extensions de fichiers volumineux pour Git LFS
GIT_LFS_ATTRIBUTES = "*.exe\n*.zip\n*.rar\n*.7z\n*.mp4\n*.mov\n*.jpg\n*.png\n*.psd\n*.ai\n*.pdf\n*.blend\n"


def charger_configuration():
    """Charge les paramètres de synchronisation sauvegardés."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("⚠️ Fichier de configuration corrompu. Suppression et redémarrage.")
            if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
            return None
    return None

def sauvegarder_configuration(repo_name, local_path, login):
    """Sauvegarde le nom du dépôt, le chemin local et le login."""
    config = {
        "repo_name": repo_name,
        "local_path": local_path,
        "login": login
    }
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)

def verifier_dependances_externes():
    """Vérifie si les commandes Git et Git LFS sont accessibles."""
    status = True
    messages = []
    try:
        subprocess.run(['git', '--version'], check=True, capture_output=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        messages.append("❌ Git n'est pas installé ou n'est pas accessible. Git est OBLIGATOIRE pour la synchronisation.")
        status = False
    try:
        subprocess.run(['git', 'lfs', 'version'], check=True, capture_output=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        messages.append("⚠️ Git LFS (Large File Storage) n'est pas installé. Les fichiers volumineux (>100 Mo) ne seront pas gérés correctement par GitHub.")
    return True if status and not messages else "\n".join(messages)

def demander_et_tester_token(token_to_test):
    """Teste la validité du PAT."""
    if not token_to_test: return False
    headers = {"Authorization": f"token {token_to_test}", "Accept": "application/vnd.github.v3+json"}
    try:
        response = requests.get(f"{GITHUB_API_URL}/user", headers=headers)
        if response.status_code == 200:
            scopes = response.headers.get('X-OAuth-Scopes', '').split(', ')
            if 'repo' in scopes and 'delete_repo' in scopes:
                return response.json()['login']
            else:
                return "PERMISSIONS_MISSING"
        else:
            return False
    except requests.exceptions.RequestException:
        return False

def charger_token():
    """Charge le token sauvegardé, si il existe."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            return f.read().strip()
    return None

def sauvegarder_token(token):
    """Sauvegarde le token dans le fichier local."""
    with open(TOKEN_FILE, 'w') as f:
        f.write(token)

# ======================================================================
# --- FONCTIONS DE GESTION GITHUB ---
# ======================================================================

def creer_nouveau_depot(token, nom_depot):
    """Crée un nouveau dépôt sur GitHub avec initialisation automatique (README.md)."""
    url = f"{GITHUB_API_URL}/user/repos"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    # auto_init: True pour que GitHub crée la branche main et le README.md.
    data = {"name": nom_depot, "private": True, "auto_init": True}

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        return response.json()['clone_url']
    elif response.status_code == 422:
        return "EXISTS"
    else:
        return False

def chercher_depot_existant(token, nom_depot):
    """Cherche un dépôt existant de l'utilisateur."""
    user_data = requests.get(f'{GITHUB_API_URL}/user', headers={'Authorization': f'token {token}'}).json()
    if 'login' not in user_data:
        return False
    url = f"{GITHUB_API_URL}/repos/{user_data['login']}/{nom_depot}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.com+json"}
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()['clone_url']
    return False

# ======================================================================
# --- FONCTIONS DE GESTION GIT LOCALE (Robuste) ---
# ======================================================================

def importer_git_dependances():
    """Importe Repo et GitCommandError."""
    try:
        from git import Repo, GitCommandError
        return Repo, GitCommandError
    except ImportError as e:
        print(f"Erreur d'initialisation de GitPython: {e}")
        return None, None
    except Exception as e:
        print(f"Erreur inconnue lors de l'import de GitPython: {e}")
        return None, None


def configurer_git_local(repo_url, chemin_local, token, login, repo_name, est_nouvelle_sync):
    """
    Initialise, clone, ou configure le dépôt Git local, en forçant la branche 'main'.
    Après le clonage via HTTPS (pour l'authentification), la remote est basculée en SSH.
    """
    Repo, GitCommandError = importer_git_dependances()
    if not Repo: return False

    auth_repo_url = repo_url.replace('https://github.com/', f'https://oauth2:{token}@github.com/')
    ssh_url = f"git@github.com:{login}/{repo_name}.git"

    # Le mode `est_nouvelle_sync` est déprécié. On clone toujours.
    if os.path.exists(chemin_local) and os.listdir(chemin_local):
         return "CLONE_ERROR"
    try:
        # 1. Clonage via HTTPS avec le token pour l'authentification
        print("Clonage du dépôt via HTTPS...")
        repo = Repo.clone_from(auth_repo_url, chemin_local)

        # 2. Basculement de la remote 'origin' vers SSH
        print("Basculement de la remote 'origin' vers l'URL SSH...")
        if 'origin' in [remote.name for remote in repo.remotes]:
            repo.remote('origin').set_url(ssh_url)
            print(f"✅ Remote 'origin' configurée pour utiliser SSH: {ssh_url}")
        else:
            # Ce cas est peu probable après un clonage, mais par sécurité
            repo.create_remote('origin', ssh_url)

    except GitCommandError:
        print("❌ Erreur de clonage. Vérifiez le nom du dépôt, vos droits d'accès ou si le dossier local est bien vide.")
        return "CLONE_ERROR"
    except Exception as e:
        print(f"❌ Erreur inattendue lors du clonage : {e}")
        return False

    git_attributes_path = os.path.join(chemin_local, '.gitattributes')
    if not os.path.exists(git_attributes_path):
        with open(git_attributes_path, 'w') as f:
            f.write(GIT_LFS_ATTRIBUTES)
    return repo


def verifier_et_mettre_a_jour_lfs(repo):
    """
    Vérifie les fichiers non suivis ou modifiés pour de nouvelles extensions de fichiers volumineux
    et met à jour .gitattributes de manière préventive.
    """
    Repo, GitCommandError = importer_git_dependances()
    if not Repo: return

    try:
        # Fichiers non suivis (nouveaux fichiers)
        untracked_files = repo.untracked_files

        # Fichiers modifiés mais pas encore "staged"
        modified_files = [item.a_path for item in repo.index.diff(None)]

        all_files_to_check = untracked_files + modified_files

        if not all_files_to_check:
            return # Pas de fichiers à vérifier

        git_attributes_path = os.path.join(repo.working_dir, '.gitattributes')

        # Charger les extensions déjà suivies par LFS
        tracked_extensions = set()
        if os.path.exists(git_attributes_path):
            with open(git_attributes_path, 'r') as f:
                for line in f:
                    if 'filter=lfs' in line:
                        # Extrait l'extension, ex: "*.blend" -> ".blend"
                        ext = line.split(' ')[0].replace('*', '').strip()
                        if ext:
                            tracked_extensions.add(ext)

        new_extensions_to_track = set()

        # Définir ici les extensions à ignorer (par exemple, les fichiers texte)
        ignored_extensions = {'.txt', '.md', '.json', '.py', '.js', '.html', '.css'}

        for file_path in all_files_to_check:
            extension = os.path.splitext(file_path)[1].lower()

            # Vérifier si l'extension est non vide, pas déjà suivie et pas dans les ignorés
            if extension and extension not in tracked_extensions and extension not in ignored_extensions:
                full_path = os.path.join(repo.working_dir, file_path)
                # On ne vérifie la taille que si le fichier existe réellement
                if os.path.exists(full_path) and os.path.getsize(full_path) > 10 * 1024 * 1024: # 10 MB
                    new_extensions_to_track.add(extension)

        if new_extensions_to_track:
            print(f"ℹ️ Détection de nouvelles extensions de fichiers volumineux : {new_extensions_to_track}")
            with open(git_attributes_path, 'a') as f:
                f.write("\n# Auto-ajout préventif par SyncTool\n")
                for ext in new_extensions_to_track:
                    f.write(f"*{ext} filter=lfs diff=lfs merge=lfs -text\n")

            # Créer un commit séparé pour le .gitattributes
            repo.index.add([git_attributes_path])
            try:
                repo.index.commit("Mise à jour LFS: Ajout de nouvelles extensions de fichiers")
                print("✅ .gitattributes mis à jour et commit préventif créé.")
            except GitCommandError as e:
                if "Hook" in str(e) and "failed" in str(e):
                    print(f"⚠️ Avertissement: Le hook de pre-commit a échoué mais sera ignoré. Erreur: {e}")
                else:
                    raise e

            # On pousse ce changement immédiatement pour s'assurer que le serveur est au courant
            try:
                repo.remote('origin').push('main')
                print("✅ Push de la mise à jour de .gitattributes réussi.")
            except GitCommandError as e:
                print(f"⚠️ Échec du push préventif pour .gitattributes: {e}")


    except Exception as e:
        print(f"❌ Erreur lors de la vérification préventive LFS : {e}")

def gerer_erreur_lfs_apres_push(repo, chemin_fichier_local):
    """Auto-correction (réactive) : Ajoute l'extension d'un fichier problématique au .gitattributes après un échec de push."""

    Repo, GitCommandError = importer_git_dependances()
    if not Repo: return False

    try:
        nom_fichier = os.path.basename(chemin_fichier_local)
        extension = os.path.splitext(nom_fichier)[1].lower()

        if not extension:
            print(f"❌ Impossible d'identifier l'extension du fichier : {nom_fichier}")
            return False

        lfs_entry = f"*{extension} filter=lfs diff=lfs merge=lfs -text"

        git_attributes_path = os.path.join(repo.working_dir, '.gitattributes')
        content = ""
        if os.path.exists(git_attributes_path):
             with open(git_attributes_path, 'r') as f:
                content = f.read()

        if lfs_entry.split(' ')[0] not in content:
            with open(git_attributes_path, 'a') as f:
                f.write(f"\n# Auto-ajout par SyncTool pour gérer LFS:\n{lfs_entry}\n")

            print(f"✅ Auto-correction: Ajout de '{extension}' au .gitattributes pour LFS.")

            repo.index.add([".gitattributes"])
            try:
                repo.index.commit(f"Auto-correction: Ajout de {extension} à LFS.")
            except GitCommandError as e:
                if "Hook" in str(e) and "failed" in str(e):
                    print(f"⚠️ Avertissement: Le hook de pre-commit a échoué mais sera ignoré. Erreur: {e}")
                else:
                    raise e

            return True
        return False

    except Exception as e:
        print(f"❌ Erreur lors de l'auto-correction LFS : {e}")
        return False


def synchroniser_changement(repo, commit_message):
    """
    Ajoute, commit et pousse les changements.
    """
    Repo, GitCommandError = importer_git_dependances()
    if not Repo: return

    max_retries = 2

    for attempt in range(max_retries):
        try:
            print(f"\n[SYNC] Tentative {attempt + 1}: {commit_message}")

            # 0. Vérification LFS préventive
            verifier_et_mettre_a_jour_lfs(repo)

            # 1. Tenter le pull avec gestion des conflits
            try:
                print("Tentative de pull depuis 'origin/main'...")
                repo.remotes.origin.pull('main')
                print("Pull réussi.")
            except GitCommandError as e:
                error_output = str(e.stderr).lower()
                if "conflict" in error_output or "merge" in error_output:
                    print("⚠️ Conflit de fusion détecté. Forçage de l'alignement avec le dépôt distant...")
                    try:
                        # "Le distant a raison" : on fetch et on reset --hard
                        repo.remotes.origin.fetch()
                        repo.git.reset('--hard', 'origin/main')
                        print("✅ Le dépôt local a été forcé à l'état de 'origin/main'.")
                    except GitCommandError as reset_e:
                        print(f"❌ Échec du reset --hard après conflit : {reset_e}")
                        # En cas d'échec du reset, il vaut mieux s'arrêter pour éviter la corruption
                        return
                elif "could not read from remote repository" in error_output:
                    print(f"❌ Erreur de Pull: Impossible de lire le dépôt distant. Vérifiez la connexion et la clé SSH.")
                elif "fatal: couldn't find remote ref main" not in error_output:
                    print(f"⚠️ Avertissement lors du pull (non-conflit) : {e.stderr.strip()}")

            # 2. Ajouter les fichiers à l'index
            repo.index.add(['.'])

            # 3. Vérification des changements et du commit
            has_initial_commit = True
            try:
                repo.git.rev_parse('--verify', 'HEAD')
            except GitCommandError:
                has_initial_commit = False

            if not has_initial_commit or repo.index.diff("HEAD"):

                try:
                    repo.index.commit(commit_message)
                    print("Commit local effectué.")
                except GitCommandError as e:
                    if "Hook" in str(e) and "failed" in str(e):
                        print(f"⚠️ Avertissement: Le hook de pre-commit a échoué mais sera ignoré. Erreur: {e}")
                    else:
                        raise e

                # --- Poussée LFS (Doit passer en premier) ---
                try:
                    repo.git.lfs('push', 'origin', 'main')
                    print("Push LFS préliminaire effectué.")
                except GitCommandError as e:
                    print(f"⚠️ Avertissement Push LFS : {e.stderr.strip()}")
                # ----------------------------------------------

                # Le push final
                repo.remote('origin').push('main', force=True)
                print("✅ Push réussi.")

                # --- LIBÉRATION CRITIQUE DES RESSOURCES ---
                try:
                    repo.close()
                    gc.collect()
                except Exception:
                    pass
                # ----------------------------------------

                return # SORTIE NORMALE

            else:
                if has_initial_commit and attempt == 0 and commit_message not in ["Initialisation de la synchronisation (via GUI)", "Initialisation par clonage"]:
                    print("Pas de changement détecté.")
                    return

        except GitCommandError as e:
            error_message = str(e.stderr).lower()

            # --- LOGIQUE D'AUTO-CORRECTION LFS/GRANDS FICHIERS ---
            if "file size exceeds" in error_message or "rpc failed" in error_message or "remote end hung up unexpectedly" in error_message:
                print(f"❌ Erreur de Push : {e}")

                nom_fichier = None
                try:
                    # Tente d'identifier le fichier à l'origine du problème
                    nom_fichier = commit_message.split(':')[-1].strip()
                    chemin_fichier_local = os.path.join(repo.working_dir, nom_fichier)
                except Exception:
                    pass

                if nom_fichier and gerer_erreur_lfs_apres_push(repo, chemin_fichier_local):
                    print("🔄 Tentative de relance après auto-correction LFS...")
                    continue

            # Si l'erreur est critique ou si l'auto-correction n'a pas pu être appliquée
            print(f"❌ Erreur lors de la synchronisation (Git) : {e}. CONFLIT POSSIBLE.")

            # --- LIBÉRATION CRITIQUE DES RESSOURCES EN CAS D'ERREUR ---
            try:
                repo.close()
                gc.collect()
            except Exception:
                pass
            # ---------------------------------------------

            return

        except Exception as e:
            print(f"❌ Erreur inattendue de synchronisation : {e}")

            # --- LIBÉRATION CRITIQUE DES RESSOURCES EN CAS D'ERREUR INATTENDUE ---
            try:
                repo.close()
                gc.collect()
            except Exception:
                pass
            # ---------------------------------------------

            return

    # Si on sort de la boucle sans succès après les tentatives
    print("❌ Échec de la synchronisation après les tentatives d'auto-correction.")


# ======================================================================
# --- LOGIQUE DE SURVEILLANCE (WATCHDOG) ---
# ======================================================================

class SyncHandler(FileSystemEventHandler):
    """Gère les événements de changement de fichier avec un mécanisme de debounce."""
    def __init__(self, repo, delay=3.0):
        self.repo = repo
        self.delay = delay
        self.timer = None
        self.lock = threading.Lock()
        super().__init__()

    def on_any_event(self, event):
        if event.is_directory:
            return
        if '.git' in event.src_path or '.gitattributes' in event.src_path or TOKEN_FILE in event.src_path or CONFIG_FILE in event.src_path:
            return

        # Annuler le timer précédent et en créer un nouveau (debounce)
        with self.lock:
            if self.timer:
                self.timer.cancel()

            self.timer = threading.Timer(self.delay, self._trigger_sync)
            self.timer.start()

    def _trigger_sync(self):
        """La fonction qui est appelée après le délai du debounce."""
        print("\n[DEBOUNCE] Délai écoulé. Lancement de la synchronisation...")
        # Utiliser un message de commit générique car plusieurs fichiers ont pu changer
        commit_message = "Synchronisation automatique des changements"
        synchroniser_changement(self.repo, commit_message)


def surveiller_et_synchroniser(repo, chemin_local):
    """Lance le système de surveillance continue."""

    # --- CORRECTIONS CRITIQUES AVANT DE DÉMARRER ---

    # 1. Configuration LFS : Installe les hooks et désactive le verrouillage LFS
    try:
        repo.git.lfs('install')
        repo.git.config('--local', f'lfs.{repo.remotes.origin.url}.info/lfs.locksverify', 'false')
        print("✅ Configuration Git LFS finalisée (Hooks installés, Locking désactivé).")
    except Exception as e:
        print(f"⚠️ Avertissement configuration LFS: {e}")

    # 2. Agent SSH : S'assure que la clé est dans l'agent
    try:
        # Tente de démarrer et d'ajouter la clé via un script PowerShell
        subprocess.run([
            'powershell',
            '-Command',
            'If (-NOT (Get-Service ssh-agent -ErrorAction SilentlyContinue)) { Set-Service -StartupType Manual -Name ssh-agent }; Start-Service ssh-agent; ssh-add $env:USERPROFILE\\.ssh\\id_ed25519'
        ], check=False, timeout=10)
        print("✅ Tentative de chargement de la clé SSH dans l'agent réussie.")
    except Exception as e:
        print(f"⚠️ Avertissement Agent SSH: Échec de la commande PowerShell. Assurez-vous d'avoir entré la passphrase manuellement une fois.")

    print(f"\n[INFO] Le dossier '{chemin_local}' est surveillé.")

    event_handler = SyncHandler(repo)
    observer = Observer()
    observer.schedule(event_handler, chemin_local, recursive=True)
    observer.start()

    Repo, _ = importer_git_dependances()

    try:
        while True:
            time.sleep(60)
            if Repo:
                try:
                    repo.remotes.origin.pull('main')
                except Exception:
                    pass

    except KeyboardInterrupt:
        observer.stop()
        print("\nArrêt de la surveillance.")

    observer.join()

# ======================================================================
# --- CLASSE DE L'APPLICATION GUI (CustomTkinter) ---
# ======================================================================

class SyncApp(ctk.CTk):
    """Classe principale de l'application de synchronisation."""
    def __init__(self):
        super().__init__()

        self.title("Mon Partenaire Sync - GitHub")
        self.geometry("600x450")
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.token = None
        self.repo = None
        self.login = None
        self.chemin_local = None
        self.systray_icon = None
        self.log_text = None
        self.original_stdout = None

        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        dependance_status = verifier_dependances_externes()

        # --- LOGIQUE AU DÉMARRAGE ---
        if dependance_status is not True:
            self.show_error_screen(dependance_status)
        else:
            config = charger_configuration()
            token = charger_token()

            if config and token:
                self.token = token
                self.login = config.get('login')
                self._start_auto_sync_thread(config['repo_name'], config['local_path'])
            else:
                self.show_auth_screen()

        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.observer = None

    # --- MÉTHODES PYSTRAY ET FERMETURE ---

    def hide_to_tray(self):
        """Cache la fenêtre et crée l'icône dans la barre d'état système."""
        self.withdraw()

        if not self.systray_icon:
            icon_image = Image.new('RGB', (64, 64), 'blue')

            menu = (
                pystray.MenuItem('Afficher', self.show_from_tray),
                pystray.MenuItem('Quitter', self.quit_app)
            )

            self.systray_icon = pystray.Icon(
                'sync_tool',
                icon_image,
                'SyncTool Actif',
                menu
            )

            self.systray_icon.run_detached()

    def show_from_tray(self, icon, item):
        """Affiche la fenêtre principale."""
        self.deiconify()
        self.lift()
        self.attributes('-topmost', True)
        self.attributes('-topmost', False)


    def quit_app(self, icon, item):
        """Arrête tout proprement."""
        icon.stop()
        self.on_closing()

    def on_closing(self):
        """Arrête l'observateur watchdog et ferme l'application."""

        if self.original_stdout is not None:
             sys.stdout = self.original_stdout
             self.original_stdout = None

        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception:
                pass

        if self.systray_icon:
            self.systray_icon.stop()

        self.destroy()

    def clear_frame(self):
        for widget in self.main_frame.winfo_children():
            widget.destroy()

    def update_status_label(self, label, message, color="white"):
        self.after(0, lambda: label.configure(text=message, text_color=color))

    # --- Logique de relance automatique ---
    def _start_auto_sync_thread(self, repo_name, local_path):
        self.clear_frame()
        self.auto_sync_status_label = ctk.CTkLabel(self.main_frame, text="", text_color="white")
        self.auto_sync_status_label.pack(pady=20)

        self.update_status_label(self.auto_sync_status_label, f"Relance automatique du dépôt '{repo_name}'...", "yellow")

        threading.Thread(target=self._run_auto_sync, args=(repo_name, local_path)).start()

    def _run_auto_sync(self, repo_name, local_path):

        clone_url = chercher_depot_existant(self.token, repo_name)
        if not clone_url:
            self.after(0, lambda: self.update_status_label(self.auto_sync_status_label, "❌ Erreur de relance. Dépôt non trouvé ou Token invalide.", "red"))
            return

        try:
            Repo, _ = importer_git_dependances()
            if not Repo:
                 self.after(0, lambda: self.update_status_label(self.auto_sync_status_label, "❌ Erreur critique : Le programme ne peut pas initialiser la bibliothèque GitPython.", "red"))
                 return

            repo = Repo(local_path)
            self.repo = repo
            self.chemin_local = local_path

            self.after(0, self.show_sync_running_screen)

        except Exception:
            self.after(0, lambda: self.update_status_label(self.auto_sync_status_label, f"❌ Erreur de relance. Le dossier local '{local_path}' est manquant ou corrompu. Veuillez recommencer.", "red"))


    # --- VUES (Erreur, Auth, Mode, Config) ---

    def show_error_screen(self, error_message):
        self.clear_frame()
        if self.original_stdout is not None:
             sys.stdout = self.original_stdout
             self.original_stdout = None

        is_git_missing = "❌ Git n'est pas installé" in error_message
        is_lfs_missing = "⚠️ Git LFS" in error_message

        ctk.CTkLabel(self.main_frame, text="🛑 DÉPENDANCES MANQUANTES 🛑", font=ctk.CTkFont(size=24, weight="bold"), text_color="red").pack(pady=20)
        ctk.CTkLabel(self.main_frame, text="Le programme ne peut pas démarrer :", text_color="white").pack(pady=10)
        ctk.CTkLabel(self.main_frame, text=error_message, text_color="yellow", wraplength=500, justify="center").pack(pady=15)

        ctk.CTkLabel(self.main_frame, text="Veuillez installer les outils manquants :", text_color="cyan").pack(pady=(10, 5))

        if is_git_missing:
            ctk.CTkButton(self.main_frame, text="Installer Git (Obligatoire)", command=lambda: webbrowser.open("https://git-scm.com/"), fg_color="#F05032").pack(pady=5)

        if is_lfs_missing:
            ctk.CTkButton(self.main_frame, text="Installer Git LFS (Recommandé)", command=lambda: webbrowser.open("https://git-lfs.com/"), fg_color="#F05032").pack(pady=5)

        ctk.CTkButton(self.main_frame, text="Quitter l'application", command=self.destroy, fg_color="red").pack(pady=20)


    def show_auth_screen(self):
        self.clear_frame()
        if self.original_stdout is not None:
             sys.stdout = self.original_stdout
             self.original_stdout = None

        ctk.CTkLabel(self.main_frame, text="🚀 Mon Partenaire Sync 🚀", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=10)
        ctk.CTkLabel(self.main_frame, text="Étape 1 : Authentification GitHub (PAT)", text_color="yellow").pack(pady=(10, 5))
        ctk.CTkLabel(self.main_frame, text="Instructions :\n1. Lien PAT : https://github.com/settings/tokens/new\n2. Cocher OBLIGATOIREMENT 'repo' et 'delete_repo'.", wraplength=500).pack(pady=5)

        self.pat_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Personal Access Token (PAT)", width=400)
        self.pat_entry.pack(pady=10)
        ctk.CTkButton(self.main_frame, text="Tester et Sauvegarder le Token", command=self._start_auth_thread).pack(pady=10)

        token_local = charger_token()
        if token_local:
             self.pat_entry.insert(0, token_local)
             ctk.CTkLabel(self.main_frame, text="Token trouvé localement. Cliquez pour vérifier.", text_color="gray").pack(pady=5)

        self.auth_status_label = ctk.CTkLabel(self.main_frame, text="", text_color="white")
        self.auth_status_label.pack(pady=10)

    def _start_auth_thread(self):
        token = self.pat_entry.get()
        if not token:
             self.auth_status_label.configure(text="Veuillez entrer un Token.", text_color="red")
             return
        self.update_status_label(self.auth_status_label, "Vérification en cours...", "yellow")
        threading.Thread(target=self._run_auth_check, args=(token,)).start()

    def _run_auth_check(self, token):
        result = demander_et_tester_token(token)
        if result == "PERMISSIONS_MISSING":
            self.update_status_label(self.auth_status_label, "❌ Permissions manquantes.", "red")
        elif result:
            sauvegarder_token(token)
            self.token = token
            self.login = result
            self.update_status_label(self.auth_status_label, f"✅ Authentification OK pour {result}!", "green")
            self.after(1000, self.show_mode_choice)
        else:
            self.update_status_label(self.auth_status_label, "❌ Token invalide, expiré ou erreur réseau.", "red")

    def show_mode_choice(self):
        self.clear_frame()
        if self.original_stdout is not None:
             sys.stdout = self.original_stdout
             self.original_stdout = None

        ctk.CTkLabel(self.main_frame, text="Étape 2 : Choix du Mode", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=20)

        ssh_warning_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        ssh_warning_frame.pack(pady=(0, 10), padx=10)
        ctk.CTkLabel(ssh_warning_frame, text="⚠️ Important : Cette application utilise SSH pour les transferts de fichiers.\nVous devez avoir une clé SSH configurée sur votre compte GitHub.", wraplength=500, justify="center", text_color="yellow").pack()
        ctk.CTkButton(ssh_warning_frame, text="Comment configurer une clé SSH ?", command=lambda: webbrowser.open("https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account"), fg_color="transparent", border_width=1, border_color="yellow").pack(pady=5)


        ctk.CTkButton(self.main_frame, text="1. Nouvelle Synchronisation (Créer un nouveau dépôt)", command=self.show_new_sync_config, width=350, height=50).pack(pady=15)
        ctk.CTkButton(self.main_frame, text="2. Synchroniser un dépôt existant (Clonage)", command=self.show_existing_sync_config, width=350, height=50).pack(pady=15)
        ctk.CTkButton(self.main_frame, text="Retour à l'authentification", command=self.show_auth_screen, fg_color="gray").pack(pady=20)

    def show_new_sync_config(self):
        self.clear_frame()
        ctk.CTkLabel(self.main_frame, text="Étape 3 : Créer une Nouvelle Synchronisation", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        ctk.CTkLabel(self.main_frame, text="Nom du Dépôt GitHub :").pack(pady=(10, 0))
        self.new_repo_name_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Ex: MonDossierSync", width=350)
        self.new_repo_name_entry.pack(pady=5)
        ctk.CTkLabel(self.main_frame, text="Chemin du Dossier Local DESTINATION (Doit être VIDE) :").pack(pady=(10, 0))
        self.new_local_path_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Ex: C:\\Users\\...\\NouveauDossierVide", width=350)
        self.new_local_path_entry.pack(pady=5)
        ctk.CTkButton(self.main_frame, text="Lancer la Nouvelle Synchronisation", command=self._start_new_sync_thread, fg_color="green").pack(pady=20)
        self.status_label_sync = ctk.CTkLabel(self.main_frame, text="", text_color="white")
        self.status_label_sync.pack(pady=10)
        ctk.CTkButton(self.main_frame, text="< Retour", command=self.show_mode_choice, fg_color="gray").pack(pady=5)

    def _start_new_sync_thread(self):
        repo_name = self.new_repo_name_entry.get()
        local_path = self.new_local_path_entry.get()
        if not repo_name or not local_path:
            self.update_status_label(self.status_label_sync, "Veuillez remplir tous les champs.", "red")
            return

        if os.path.exists(local_path) and os.listdir(local_path):
            self.update_status_label(self.status_label_sync, "❌ Le dossier local doit être VIDE pour le clonage.", "red")
            return

        self.update_status_label(self.status_label_sync, "Démarrage de la nouvelle synchronisation...", "yellow")
        threading.Thread(target=self._run_new_sync, args=(repo_name, local_path)).start()

    def _run_new_sync(self, repo_name, local_path):

        self.update_status_label(self.status_label_sync, "Création du dépôt GitHub avec README.md...", "yellow")

        # 1. Création du dépôt distant (avec auto_init=True)
        clone_url = creer_nouveau_depot(self.token, repo_name)

        if clone_url == "EXISTS":
            self.update_status_label(self.status_label_sync, "❌ Le dépôt existe déjà sur GitHub. Choisissez un nom différent ou utilisez le mode 'Clonage'.", "red")
            return
        if not clone_url:
            self.update_status_label(self.status_label_sync, "❌ Échec de la création du dépôt GitHub.", "red")
            return

        self.update_status_label(self.status_label_sync, "Clonage du nouveau dépôt localement...", "yellow")

        # 2. Clonage des fichiers (y compris le README.md créé par GitHub)
        # On utilise configurer_git_local en mode CLONAGE (est_nouvelle_sync=False)
        self.repo = configurer_git_local(clone_url, local_path, self.token, self.login, repo_name, est_nouvelle_sync=False)

        if self.repo == "CLONE_ERROR":
            self.update_status_label(self.status_label_sync, "❌ Le dossier de destination DOIT être VIDE pour le clonage.", "red")
            return
        if not self.repo:
            self.update_status_label(self.status_label_sync, "❌ Échec du clonage. Problème de droits ou d'accès.", "red")
            return

        print("✅ Dépôt créé et cloné avec succès. Il contient déjà le fichier README.md.")

        self.chemin_local = local_path
        sauvegarder_configuration(repo_name, local_path, self.login)
        self.after(0, self.show_sync_running_screen)

    def show_existing_sync_config(self):
        self.clear_frame()
        ctk.CTkLabel(self.main_frame, text="Étape 3 : Synchroniser un Dépôt Existant", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=10)
        ctk.CTkLabel(self.main_frame, text="Nom du Dépôt GitHub Existant :").pack(pady=(10, 0))
        self.existing_repo_name_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Ex: MonDossierSync", width=350)
        self.existing_repo_name_entry.pack(pady=5)
        ctk.CTkLabel(self.main_frame, text="Chemin du Dossier Local DESTINATION (Doit être VIDE) :").pack(pady=(10, 0))
        self.existing_local_path_entry = ctk.CTkEntry(self.main_frame, placeholder_text="Ex: C:\\Users\\...\\NouveauDossierVide", width=350)
        self.existing_local_path_entry.pack(pady=5)
        ctk.CTkButton(self.main_frame, text="Cloner et Lancer la Synchronisation", command=self._start_existing_sync_thread, fg_color="blue").pack(pady=20)
        self.status_label_sync = ctk.CTkLabel(self.main_frame, text="", text_color="white")
        self.status_label_sync.pack(pady=10)
        ctk.CTkButton(self.main_frame, text="< Retour", command=self.show_mode_choice, fg_color="gray").pack(pady=5)

    def _start_existing_sync_thread(self):
        repo_name = self.existing_repo_name_entry.get()
        local_path = self.existing_local_path_entry.get()
        if not repo_name or not local_path:
            self.update_status_label(self.status_label_sync, "Veuillez remplir tous les champs.", "red")
            return
        self.update_status_label(self.status_label_sync, "Démarrage du clonage et de la synchronisation...", "yellow")
        threading.Thread(target=self._run_existing_sync, args=(repo_name, local_path)).start()

    def _run_existing_sync(self, repo_name, local_path):

        self.update_status_label(self.status_label_sync, "Recherche du dépôt GitHub...", "yellow")
        clone_url = chercher_depot_existant(self.token, repo_name)
        if not clone_url:
            self.update_status_label(self.status_label_sync, "❌ Dépôt GitHub non trouvé.", "red")
            return

        self.update_status_label(self.status_label_sync, "Clonage des fichiers...", "yellow")
        self.repo = configurer_git_local(clone_url, local_path, self.token, self.login, repo_name, est_nouvelle_sync=False)

        if self.repo == "CLONE_ERROR":
            self.update_status_label(self.status_label_sync, "❌ Le dossier de destination doit être VIDE.", "red")
            return
        if not self.repo:
            self.update_status_label(self.status_label_sync, "❌ Échec du clonage. Problème de droits ou d'accès.", "red")
            return

        self.chemin_local = local_path
        sauvegarder_configuration(repo_name, local_path, self.login)
        self.after(0, self.show_sync_running_screen)

    def show_sync_running_screen(self):
        self.clear_frame()

        if self.original_stdout is not None:
             sys.stdout = self.original_stdout
             self.original_stdout = None

        ctk.CTkLabel(self.main_frame, text="✅ SYNCHRONISATION ACTIVE", font=ctk.CTkFont(size=24, weight="bold"), text_color="green").pack(pady=10)
        ctk.CTkLabel(self.main_frame, text=f"Dossier surveillé : {self.chemin_local}", text_color="cyan").pack(pady=5)
        ctk.CTkLabel(self.main_frame, text="Le détail des opérations est visible ci-dessous.", text_color="white").pack(pady=5)
        ctk.CTkLabel(self.main_frame, text="Pour arrêter, utilisez l'icône dans la barre d'état.", text_color="red").pack(pady=5)

        self.log_text = ctk.CTkTextbox(self.main_frame, width=550, height=200)
        self.log_text.pack(pady=10, padx=10)

        self.original_stdout = sys.stdout
        sys.stdout = ConsoleRedirector(self.log_text, self.original_stdout)

        threading.Thread(target=surveiller_et_synchroniser, args=(self.repo, self.chemin_local)).start()

        self.after(1000, self.hide_to_tray)

# ======================================================================
# --- POINT D'ENTRÉE ---
# ======================================================================

if __name__ == '__main__':
    app = SyncApp()
    app.mainloop()