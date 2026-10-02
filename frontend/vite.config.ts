import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

// Bind to 127.0.0.1 explicitly: by default Vite listens on "localhost", which on Windows can mean IPv6 only, so the
// documented address http://127.0.0.1:5173 (and the origin the backend's CORS allows) would refuse connections.
export default defineConfig({plugins:[react()],server:{host:'127.0.0.1',port:5173,strictPort:true}});
