"""Route-only adapters for the additive Agent repair contract."""
from core.contracts import AGENT_MASTER_ENSURE_ROUTE, AGENT_PENDING_REPAIR_ROUTE, fail


def build_agent_post_routes(connection_factory, repository, log, knowledge_hook=None):
    def run(operation, context, body):
        connection = connection_factory()
        try:
            result = operation(connection, body)
            if operation == repository.agent_master_ensure and knowledge_hook is not None:
                result["data"]["knowledge_saved"] = knowledge_hook(result, body)
            log("INFO", context + " " + str(result.get("data", {}).get("key", body.get("id", ""))))
            return result
        except ValueError as error:
            log("WARN", context + " rejected: " + str(error))
            return fail(str(error), "repair_rejected")
        finally:
            connection.close()
    return {
        AGENT_MASTER_ENSURE_ROUTE: lambda body: run(repository.agent_master_ensure, "Agent master ensure", body),
        AGENT_PENDING_REPAIR_ROUTE: lambda body: run(repository.agent_pending_repair, "Agent pending repair", body),
    }
