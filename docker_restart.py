"""
Docker restart utility for handling browser session invalidation
"""
import subprocess
import time
import logging

def restart_docker_services():
    """Restart Docker services when browser session becomes invalid"""
    try:
        logging.info("🔄 Browser session invalid - restarting Docker services...")
        
        # Get the project directory
        project_dir = "/home/ead8/Downloads/sundialapi/1743534086245-realstate-scrape"
        
        # Stop and remove containers
        logging.info("⏹️  Stopping Docker containers...")
        subprocess.run(["docker", "compose", "down", "-v"], 
                      cwd=project_dir, 
                      capture_output=True, 
                      text=True, 
                      timeout=60)
        
        # Wait a moment
        time.sleep(2)
        
        # Start containers again
        logging.info("▶️  Starting Docker containers...")
        result = subprocess.run(["docker", "compose", "up", "--build", "-d"], 
                               cwd=project_dir, 
                               capture_output=True, 
                               text=True, 
                               timeout=120)
        
        if result.returncode == 0:
            logging.info("✅ Docker services restarted successfully")
            # Wait for services to be ready
            time.sleep(10)
            return True
        else:
            logging.error(f"❌ Failed to restart Docker services: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        logging.error("❌ Docker restart timed out")
        return False
    except Exception as e:
        logging.error(f"❌ Error restarting Docker services: {e}")
        return False
