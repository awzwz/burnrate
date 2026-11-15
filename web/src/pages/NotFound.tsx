import { Link } from "react-router-dom";
import { Logo } from "../components/Bits";

export default function NotFound() {
  return (
    <div className="splash">
      <Logo size={22} />
      <p>This page doesn't exist.</p>
      <Link className="btn" to="/">
        Go to the start
      </Link>
    </div>
  );
}
